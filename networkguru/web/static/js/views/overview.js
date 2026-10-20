// Tab overview: status tiles, the list of current problems, and a card per vehicle / desk / location.

import { get } from "../api.js";
import { clear, h, setClass, setText } from "../dom.js";
import { fmtPct, fmtValue, fmtWhen, metricLevel, statusName, STATUS_LABEL, typeSpec } from "../fmt.js";
import { badge, dot, updateBadge, updateDot } from "../status.js";
import { node, rangeLabel, store, window_ } from "../store.js";
import { miniPath } from "../widgets/path.js";
import { battery, sparkline, statusStrip } from "../widgets/small.js";
import { TAB_TITLE, vehicleHops } from "./common.js";

const KIND_LABEL = { trailer: "Solar trailer", tower: "Tower", building: "Building", repeater: "Repeater", core: "Network core",
  cow: "COW (base of the 60 GHz backhaul)", server: "Server", client: "Client", vehicle: "Vehicle" };
const SPARK_LABEL = { vcu_comms_latency_ms: "Comms latency", power_soc_pct: "Battery", mobius_clients: "Clients",
  host_cpu_pct: "CPU", ping_rtt_ms: "Ping RTT" };

function metricCell(label, compId, metric) {
  const v = h("div", { class: "v num" });
  const k = h("div", { class: "k" }, label);
  const md = h("span", { class: "mdot" });
  k.append(md);
  return {
    el: h("div", {}, k, v),
    update() {
      const val = compId ? node(compId).m?.[metric] : null;
      setText(v, val == null ? "–" : fmtValue(metric, val));
      v.classList.toggle("na", val == null);
      const lvl = metricLevel(metric, val);
      setClass(md, "s-", lvl);
      md.classList.toggle("hidden", !lvl);
      md.title = lvl ? STATUS_LABEL[lvl] : "";
    },
  };
}

function compOf(g, ...types) {
  return g.components.find((c) => types.includes(c.type))?.id || null;
}

function baseCard(g, nav) {
  const b = badge(1, { small: true });
  const why = h("div", { class: "card-why" });
  const sub = h("div", { class: "card-sub" });
  const spark = h("div", { class: "spark" });
  const foot = h("span", { class: "foot-txt" });
  const hist = h("div", { class: "hist-row hidden" });
  const el = h("a", { class: "card", href: `#/${g.tab}/${g.id}` },
    h("div", { class: "card-top" }, h("div", { class: "name", text: g.name, title: g.name }), b), sub, why);
  el.addEventListener("click", (e) => { e.preventDefault(); nav(`#/${g.tab}/${g.id}`); });
  const footEl = h("div", { class: "card-foot" }, spark, foot);
  return { el, b, why, sub, spark, foot, hist, footEl };
}

function vehicleCard(g, nav) {
  const c = baseCard(g, nav);
  const vcu = compOf(g, "vcu");
  const gnss = compOf(g, "gnss");
  const radio = compOf(g, "rajant", "cell_modem");
  const cells = [
    metricCell("Comms latency", vcu || radio, vcu ? "vcu_comms_latency_ms" : "ping_rtt_ms"),
    metricCell("Comms drop", vcu, "vcu_comms_drop_pct"),
    metricCell("GNSS", gnss, "gnss_mode"),
    metricCell("VCU CPU temp", vcu, "vcu_cpu_temp_c"),
  ];
  const mp = miniPath(vehicleHops(g.id));
  c.el.append(h("div", { class: "kv" }, cells.map((x) => x.el)), mp.el, c.hist, c.footEl);
  setText(c.sub, [g.model, (g.access || "rajant") === "celona" ? "Celona LTE" : "Rajant mesh"].filter(Boolean).join(" · "));
  return {
    ...c,
    footAuto: true,
    update() {
      cells.forEach((x) => x.update());
      mp.update();
    },
  };
}

function locationCard(g, nav) {
  const c = baseCard(g, nav);
  const gx = compOf(g, "victron_cerbo");
  const dots = h("div", { class: "devdots" });
  const devs = g.components.map((x) => {
    const d = dot(1);
    dots.append(h("span", { class: "devdot", title: x.name }, d, typeSpec(x.type).short));
    return { x, d };
  });
  const pw = h("div", { class: "kv" });
  let batt = null;
  const solar = metricCell("Solar / load", null, "power_pv_w");
  const charger = metricCell("Charger", gx, "power_mppt_state");
  const uplink = compOf(g, "rajant", "unifi", "host", "gnss_base", "celona_ap");
  const rtt = metricCell("Ping RTT", uplink, "ping_rtt_ms");
  // second reading: mesh peers, else the headline of the next device (e.g. Starlink latency)
  const other = g.components.find((x) => x.id !== uplink && x.type !== "victron_cerbo");
  const otherMetric = other ? typeSpec(other.type).headline[0] : null;
  const peers = compOf(g, "rajant") ? metricCell("Mesh peers", compOf(g, "rajant"), "rajant_peers")
    : metricCell(other ? `${typeSpec(other.type).label}: ${store.meta.catalog.metrics[otherMetric]?.label || ""}` : "–",
      other?.id || null, otherMetric || "ping_rtt_ms");
  const battWrap = h("div", {});
  if (gx) {
    pw.append(h("div", {}, h("div", { class: "k", text: "Battery" }), battWrap), solar.el, charger.el, rtt.el);
  } else {
    pw.append(rtt.el, peers.el);
  }
  c.el.append(dots, pw, c.hist, c.footEl);
  setText(c.sub, KIND_LABEL[g.kind] || g.kind || "");
  return {
    ...c,
    footAuto: !gx,
    update() {
      for (const { x, d } of devs) updateDot(d, node(x.id).s, `${x.name}: ${STATUS_LABEL[statusName(node(x.id).s)]}`);
      rtt.update();
      peers.update();
      if (gx) {
        const m = node(gx).m || {};
        const nb = battery(m.power_soc_pct);
        battWrap.replaceChildren(nb);
        charger.update();
        const v = solar.el.querySelector(".v");
        setText(v, m.power_pv_w == null ? "–" : `${fmtValue("power_pv_w", m.power_pv_w, { unit: false })} / ${fmtValue("power_load_w", m.power_load_w)}`);
        v.classList.toggle("na", m.power_pv_w == null);
        solar.el.querySelector(".mdot").classList.add("hidden");
        setText(c.foot, m.power_yield_today_kwh != null ? `solar today ${fmtValue("power_yield_today_kwh", m.power_yield_today_kwh)}` : "");
      }
    },
  };
}

function controlCard(g, nav) {
  const c = baseCard(g, nav);
  let cells;
  if (g.kind === "server") {
    const svc = compOf(g, "mobius_server");
    cells = [metricCell("Connected clients", svc, "mobius_clients"), metricCell("Communicating assets", svc, "mobius_assets"),
      metricCell("Server CPU", svc, "host_cpu_pct"), metricCell("RTK corrections", compOf(g, "mobius_ntrip"), "ntrip_connected")];
  } else {
    const pc = compOf(g, "mobius_client");
    cells = [metricCell("Mobius session", pc, "client_connected"), metricCell("CPU", pc, "host_cpu_pct"),
      metricCell("Memory", pc, "host_mem_used_pct"), metricCell("Disk free", pc, "host_disk_free_pct")];
  }
  c.el.append(h("div", { class: "kv" }, cells.map((x) => x.el)), c.hist, c.footEl);
  setText(c.sub, KIND_LABEL[g.kind] || "");
  return {
    ...c,
    update() {
      cells.forEach((x) => x.update());
      const pc = compOf(g, "mobius_client");
      const user = pc ? node(pc).i?.user : null;
      setText(c.foot, g.kind === "client" ? (user ? `user ${user}` : "") : (node(compOf(g, "mobius_server")).i?.version || ""));
    },
  };
}

// ---------------------------------------------------------------------------
export function overviewView(root, tabId, nav) {
  const tab = store.inv.tabs.find((t) => t.id === tabId);
  const groups = tab.groups.map((g) => store.groups[g.id]);
  const make = tabId === "vehicles" ? vehicleCard : tabId === "control" ? controlCard : locationCard;
  const cards = groups.map((g) => ({ g, card: make(g, nav) }));
  const tiles = h("div", { class: "tiles" });
  const tilesCap = h("div", { class: "tiles-cap" });
  const issues = h("ul");
  const issuesBox = h("section", { class: "issues" }, h("h2", { text: "Needs attention now" }), issues);
  const sub = h("span", { class: "sub" });
  let rangeWorst = null;              // History: worst status per group over the range
  root.append(
    h("div", { class: "page-head" }, h("h1", { text: TAB_TITLE[tabId] }), sub),
    tilesCap, tiles, issuesBox,
    h("div", { class: "grid" }, cards.map((x) => x.card.el)));

  const tileEls = {};
  for (const st of ["critical", "warning", "ok", "offline", "unknown"]) {
    const val = h("div", { class: "val num" });
    const el = h("div", { class: "tile" }, h("div", { class: "lbl" }, dot(st), STATUS_LABEL[st]), val);
    tileEls[st] = { el, val };
    tiles.append(el);
  }

  function updateIssues() {
    clear(issues);
    const rows = [];
    for (const g of groups) {
      const gn = node(g.id);
      for (const c of g.components) {
        const n = node(c.id);
        if ((n.s === 3 || n.s === 4) && !n.sup) rows.push({ g, c, n });
      }
      if ((gn.s === 3 || gn.s === 4) && !rows.some((r) => r.g === g)) rows.push({ g, c: null, n: gn });
    }
    rows.sort((a, b) => b.n.s - a.n.s || (a.n.since || 0) - (b.n.since || 0));
    if (!rows.length) {
      issues.append(h("li", { class: "none", text: "Nothing needs attention." }));
      return;
    }
    for (const r of rows.slice(0, 30)) {
      const href = r.c ? `#/c/${r.c.id}` : `#/${tabId}/${r.g.id}`;
      const a = h("a", { href }, badge(r.n.s, { small: true }),
        h("span", { text: r.c ? `${r.g.name} · ${r.c.name}` : r.g.name }),
        h("span", { class: "why", text: (r.n.r || []).join(" · ") }),
        h("span", { class: "muted when", text: r.n.since ? `since ${fmtWhen(r.n.since)}` : "" }));
      a.addEventListener("click", (e) => { e.preventDefault(); nav(href); });
      issues.append(h("li", {}, a));
    }
  }

  function update() {
    const counts = { ok: 0, warning: 0, critical: 0, offline: 0, unknown: 0 };
    for (const { g, card } of cards) {
      const n = node(g.id);
      counts[statusName(rangeWorst ? rangeWorst[g.id] : n.s)] += 1;
      updateBadge(card.b, n.s);
      setClass(card.el, "s-", statusName(n.s));
      setText(card.why, n.s === 0 ? "" : (n.r || []).slice(0, 2).join(" · "));
      card.update();
    }
    for (const [k, t] of Object.entries(tileEls)) {
      setText(t.val, String(counts[k]));
      t.el.classList.toggle("hidden", counts[k] === 0 && (k === "unknown" || k === "offline"));
    }
    setText(sub, `${groups.length} ${tabId === "vehicles" ? "vehicles" : tabId === "control" ? "stations" : "locations"} · ${rangeLabel()}`);
    setText(tilesCap, rangeWorst ? `Worst status during the ${rangeLabel()} (Offline = not in service)` : "Status now");
    updateIssues();
  }

  let timer = null;
  let gen = 0;
  async function loadHistory() {
    const my = ++gen;
    const w = window_();
    const ids = groups.map((g) => g.id).join(",");
    const reqs = [get("/api/history/sparklines", { ids, from: w.from, to: w.to })];
    if (!w.live) {
      reqs.push(get("/api/availability", { ids, from: w.from, to: w.to }));
      reqs.push(get("/api/history/status", { ids, from: w.from, to: w.to }));
    }
    let res;
    try { res = await Promise.all(reqs); } catch (e) { console.error(e); return; }
    if (my !== gen) return;
    const [sp, av, st] = res;
    if (st) {
      rangeWorst = {};
      for (const g of groups) {
        const segs = st[g.id] || [];
        const inService = segs.filter((x) => x[2] !== 1 && x[2] !== 2).map((x) => x[2]);
        rangeWorst[g.id] = inService.length ? Math.max(...inService) : segs.some((x) => x[2] === 2) ? 2 : 1;
      }
      update();
    }
    for (const { g, card } of cards) {
      const s = sp[g.id];
      card.spark.replaceChildren();
      const what = `${SPARK_LABEL[s?.metric] || s?.metric || ""}, ${rangeLabel()}`;
      if (s && s.points.length > 1) {
        card.spark.append(sparkline(s.points, { from: w.from, to: w.to, metric: s.metric }));
        card.spark.title = what;
      }
      if (card.footAuto) setText(card.foot, s && s.points.length > 1 ? what : `no data, ${rangeLabel()}`);
      if (w.live) {
        card.hist.classList.add("hidden");
        continue;
      }
      card.hist.classList.remove("hidden");
      const a = av[g.id];
      card.hist.replaceChildren(
        h("div", {}, h("div", { class: "avail num", text: fmtPct(a?.pct) }), h("div", { class: "avail-lbl", text: "available" })),
        h("div", { class: "strip-wrap" }, statusStrip(st[g.id], w.from, w.to)));
    }
  }

  update();
  loadHistory();
  if (window_().live) timer = setInterval(loadHistory, 60000);
  return {
    update,
    refreshRange() { loadHistory(); },
    destroy() { if (timer) clearInterval(timer); },
  };
}
