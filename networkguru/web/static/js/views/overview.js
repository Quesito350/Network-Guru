// Tab overview: status tiles (each one filters the cards), the list of current problems, and a card
// per vehicle / desk / location. In Live, groups that have gone quiet are left out (quiet.js).

import { get } from "../api.js";
import { h, setClass, setText } from "../dom.js";
import { fmtAgo, fmtPct, fmtValue, metricLevel, statusName, STATUS_LABEL, typeSpec } from "../fmt.js";
import { isQuiet, quietText, setShowHidden, showHidden } from "../quiet.js";
import { badge, dot, updateBadge, updateDot } from "../status.js";
import { node, rangeLabel, store, window_ } from "../store.js";
import { miniPath } from "../widgets/path.js";
import { battery, sparkline, statusStrip } from "../widgets/small.js";
import { attentionRows, fillAttention, TAB_TITLE, vehicleHops } from "./common.js";

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
  const mob = h("div", { class: "card-mobius hidden" });
  const el = h("a", { class: "card", href: `#/${g.tab}/${g.id}` },
    h("div", { class: "card-top" }, h("div", { class: "name", text: g.name, title: g.name }), b), sub, why, mob);
  el.addEventListener("click", (e) => { e.preventDefault(); nav(`#/${g.tab}/${g.id}`); });
  const footEl = h("div", { class: "card-foot" }, spark, foot);
  return { el, b, why, sub, spark, foot, hist, footEl, mob };
}

const STATES = ["critical", "warning", "ok", "offline", "unknown"];
const NOUN = { vehicles: "vehicles", control: "stations", infrastructure: "locations" };

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
  const tiles = h("div", { class: "tiles", role: "group", "aria-label": "Show only one status" });
  const tilesCap = h("div", { class: "tiles-cap" });
  const issues = h("ul");
  const issuesBox = h("section", { class: "issues" }, h("h2", { text: "Needs attention now" }), issues);
  const sub = h("span", { class: "sub" });
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const show = STATES.includes(params.get("show")) ? params.get("show") : null;    // the tile filter
  const filterBar = h("div", { class: "filter-bar hidden" });
  const quietBar = h("div", { class: "quiet-bar hidden" });
  const empty = h("p", { class: "empty-filter hidden" });
  let rangeWorst = null;              // History: worst status per group over the range
  let hiddenIds = new Set();          // Live: groups left out because they went quiet
  root.append(
    h("div", { class: "page-head" }, h("h1", { text: TAB_TITLE[tabId] }), sub),
    tilesCap, tiles, filterBar, quietBar, issuesBox, empty,
    h("div", { class: "grid" }, cards.map((x) => x.card.el)));

  // a tile is a filter: the URL keeps it (#/vehicles?show=critical), so a refresh or a link does too
  function setShow(st) {
    const [path, query] = location.hash.split("?");
    const q = new URLSearchParams(query || "");
    if (st) q.set("show", st);
    else q.delete("show");
    const qs = q.toString();
    location.hash = (path || "#/" + tabId) + (qs ? `?${qs}` : "");
  }

  const tileEls = {};
  for (const st of STATES) {
    const val = h("div", { class: "val num" });
    const el = h("button", { type: "button", class: `tile${show === st ? " on" : ""}`, "aria-pressed": String(show === st),
      title: show === st ? "Show all again" : `Show only ${STATUS_LABEL[st]}`, onclick: () => setShow(show === st ? null : st) },
    h("div", { class: "lbl" }, dot(st), STATUS_LABEL[st]), val);
    tileEls[st] = { el, val };
    tiles.append(el);
  }
  if (show) {
    filterBar.classList.remove("hidden");
    filterBar.append(h("span", { text: `Showing ${STATUS_LABEL[show]} only` }),
      h("button", { type: "button", class: "linkish", text: "Show all", onclick: () => setShow(null) }));
  }

  function updateIssues() {
    fillAttention(issues, attentionRows(groups, hiddenIds), nav);
  }

  function update() {
    const counts = { ok: 0, warning: 0, critical: 0, offline: 0, unknown: 0 };
    const hideQuiet = !showHidden(tabId);
    const quiet = new Set(groups.filter((g) => isQuiet(g)).map((g) => g.id));
    hiddenIds = hideQuiet ? quiet : new Set();
    let shown = 0;
    const now = Date.now() / 1000;
    for (const { g, card } of cards) {
      const n = node(g.id);
      const st = statusName(rangeWorst ? rangeWorst[g.id] : n.s);
      const left = hiddenIds.has(g.id);
      if (!left) counts[st] += 1;
      const visible = !left && (!show || st === show);
      if (visible) shown += 1;
      card.el.classList.toggle("hidden", !visible);
      card.el.classList.toggle("quiet", quiet.has(g.id));
      updateBadge(card.b, n.s);
      setClass(card.el, "s-", statusName(n.s));
      setText(card.why, quiet.has(g.id) ? `Hidden from Live when quiet: ${quietText(g)}`
        : n.s === 0 ? "" : (n.r || []).slice(0, 2).join(" · "));
      const mt = store.mobius?.groups?.[g.id];
      card.mob.classList.toggle("hidden", !mt);
      if (mt) setText(card.mob, `Mobius data ${fmtAgo(now - mt)}`);
      card.update();
    }
    for (const [k, t] of Object.entries(tileEls)) {
      setText(t.val, String(counts[k]));
      t.el.classList.toggle("hidden", counts[k] === 0 && (k === "unknown" || k === "offline") && show !== k);
    }
    const noun = NOUN[tabId];
    setText(sub, `${groups.length - hiddenIds.size} ${noun}${hiddenIds.size ? ` (+${hiddenIds.size} hidden)` : ""} · ${rangeLabel()}`);
    setText(tilesCap, rangeWorst ? `Worst status during the ${rangeLabel()} (Offline = not in service) - click one to show only those`
      : "Status now - click one to show only those");
    empty.classList.toggle("hidden", !show || shown > 0);
    if (show) setText(empty, `No ${STATUS_LABEL[show]} ${noun} ${rangeWorst ? `during the ${rangeLabel()}` : "right now"}.`);
    quietBar.classList.toggle("hidden", !quiet.size);
    if (quiet.size) {
      const mins = store.meta.hide_quiet?.min || 60;
      const what = `${quiet.size} ${quiet.size === 1 ? noun.replace(/s$/, "") : noun} with no answer for over ${mins >= 120 ? `${Math.round(mins / 60)} hours` : `${mins} minute${mins === 1 ? "" : "s"}`}`;
      quietBar.replaceChildren(h("span", { text: hideQuiet ? `${what} - hidden from Live.` : `${what} - shown (faded).` }),
        h("button", { type: "button", class: "linkish", text: hideQuiet ? "Show them" : "Hide them",
          onclick: () => { setShowHidden(tabId, hideQuiet); update(); } }));
    }
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
    if (w.live) rangeWorst = null;
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
