// Pieces shared by the detail pages: header, component table, chart sections, events list.

import { get } from "../api.js";
import { clear, h, setClass, setText } from "../dom.js";
import { fmtAgo, fmtPct, fmtTime, fmtValue, fmtWhen, metricLevel, statusName, STATUS_LABEL, typeSpec } from "../fmt.js";
import { badge, updateBadge } from "../status.js";
import { node, rangeLabel, store, window_ } from "../store.js";
import { chartCard } from "../widgets/chart.js";
import { statusStrip } from "../widgets/small.js";

export const TAB_TITLE = { vehicles: "Vehicles", control: "Control Room", infrastructure: "Infrastructure" };
const RADIO = ["rajant", "cell_modem"];
const ROUTER = ["mikrotik", "westermo"];

// Same chain as the server's History.path(), built from the inventory.
export function vehicleHops(gid) {
  const g = store.groups[gid];
  const access = (g.access || "rajant").toLowerCase();
  const all = Object.values(store.comps);
  const hops = [];
  const server = all.find((c) => c.type === "mobius_server");
  if (server) hops.push({ key: "server", label: "Mobius Server", comp: server.id });
  const core = Object.values(store.groups).find((x) => x.tab === "infrastructure" && x.kind === "core");
  if (core) hops.push({ key: "core", label: "Site network", group: core.id });
  const accTypes = access === "celona" ? ["celona_ap", "celona_edge"] : ["rajant"];
  hops.push({ key: "access", label: access === "celona" ? "Celona private LTE" : "Rajant mesh",
    comps: all.filter((c) => accTypes.includes(c.type) && c.tab === "infrastructure").map((c) => c.id) });
  const radio = g.components.find((c) => RADIO.includes(c.type));
  const router = g.components.find((c) => ROUTER.includes(c.type));
  if (radio) hops.push({ key: "radio", label: radio.name, comp: radio.id, edge: "ping_rtt_ms" });
  if (router) hops.push({ key: "router", label: router.name, comp: router.id, edge: "ping_rtt_ms" });
  for (const [t, edge] of [["vcu", "vcu_comms_latency_ms"], ["vai", "vai_heartbeat_age_s"]]) {
    const c = g.components.find((x) => x.type === t);
    if (c) hops.push({ key: t, label: c.name, comp: c.id, edge });
  }
  return hops;
}

export function readings(compId, metrics) {
  const n = node(compId);
  return metrics.map((m) => {
    const v = n.m?.[m];
    return { metric: m, value: v, level: metricLevel(m, v) };
  });
}

// ---------------------------------------------------------------------------
export function detailHeader({ id, title, crumbHref, crumbText, facts = [] }) {
  const b = badge(1);
  const reasons = h("ul", { class: "reasons" });
  const factsEl = h("div", { class: "facts" });
  const since = h("span", { class: "muted" });
  const hist = h("div", { class: "hist-summary hidden" });
  const el = h("section", { class: "detail-head" },
    h("a", { class: "crumb", href: crumbHref, text: `← ${crumbText}` }),
    h("div", { class: "row1" }, h("h1", { text: title }), b, since),
    factsEl, reasons, hist);
  function update() {
    const n = node(id);
    updateBadge(b, n.s);
    setClass(el, "s-", statusName(n.s));
    setText(since, n.since ? `since ${fmtWhen(n.since)}` : "");
    clear(reasons);
    for (const r of (n.r || []).slice(0, 4)) if (r) reasons.append(h("li", { text: r }));
    clear(factsEl);
    for (const [k, v] of typeof facts === "function" ? facts() : facts) {
      if (v != null && v !== "") factsEl.append(h("span", {}, `${k} `, h("b", { text: String(v) })));
    }
  }
  async function history() {
    const w = window_();
    if (w.live) { hist.classList.add("hidden"); return; }
    hist.classList.remove("hidden");
    const [av, st] = await Promise.all([
      get("/api/availability", { ids: id, from: w.from, to: w.to }),
      get("/api/history/status", { ids: id, from: w.from, to: w.to }),
    ]);
    const a = av[id];
    const worst = Math.max(...(st[id] || []).map((x) => (x[2] === 2 ? 0 : x[2])), 0);
    clear(hist);
    hist.append(
      h("div", {}, h("div", { class: "avail-lbl", text: `Availability, ${rangeLabel()}` }),
        h("div", { style: "font-size:24px;font-weight:bold", text: fmtPct(a?.pct) })),
      h("div", {}, h("div", { class: "avail-lbl", text: "Worst status" }), badge(worst, { small: true })),
      h("div", { class: "strip-wrap" }, h("div", { class: "avail-lbl", text: `${fmtTime(w.from, "full")} – ${fmtTime(w.to, "full")}` }),
        statusStrip(st[id], w.from, w.to, { height: 14 })));
  }
  return { el, update, history };
}

// ---------------------------------------------------------------------------
export function compTable(comps, nav) {
  const tbody = h("tbody");
  const rows = comps.map((c) => {
    const b = badge(1, { small: true });
    const why = h("div", { class: "why" });
    const rd = h("div", { class: "readings" });
    const seen = h("td", { class: "muted num" });
    const addr = h("span", { class: "num" });
    const tr = h("tr", { tabindex: 0 },
      h("td", {}, b),
      h("td", {}, h("div", { text: c.name }), why),
      h("td", { class: "ink2" }, typeSpec(c.type).label,
        c.link ? h("div", { class: "muted", style: "font-size:12px", text: `${c.link} link · ${c.role || "?"} end` }) : null),
      h("td", { class: "ink2" }, addr, c.address === "dhcp" ? h("span", { class: "chip", title: "Address comes from DHCP", text: "DHCP" }) : null),
      h("td", {}, rd), seen);
    tr.addEventListener("click", () => nav(`#/c/${c.id}`));
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") nav(`#/c/${c.id}`); });
    tbody.append(tr);
    return { c, b, why, rd, seen, addr };
  });
  const el = h("div", { class: "panel table-scroll" },
    h("table", { class: "comps" },
      h("thead", {}, h("tr", {}, ...["Status", "Component", "Type", "Address", "Readings", "Last seen"].map((t) => h("th", { text: t })))),
      tbody));
  function update() {
    const now = Date.now() / 1000;
    for (const r of rows) {
      const n = node(r.c.id);
      updateBadge(r.b, n.s);
      setText(r.addr, n.i?.address || r.c.host || (r.c.source === "mobius" ? "via Mobius" : "–"));
      setText(r.why, n.s === 0 ? "" : (n.r || []).slice(0, 2).join(" · "));
      clear(r.rd);
      for (const x of readings(r.c.id, typeSpec(r.c.type).headline)) {
        if (x.value == null) continue;
        r.rd.append(h("span", {}, `${shortLabel(x.metric)} `, h("b", { text: fmtValue(x.metric, x.value) }),
          x.level ? h("span", { class: `mdot s-${x.level}`, style: "margin-left:4px" }) : null));
      }
      setText(r.seen, n.seen ? (now - n.seen < 120 ? fmtAgo(now - n.seen) : fmtWhen(n.seen)) : "–");
    }
  }
  return { el, update };
}

export function shortLabel(metric) {
  const map = { ping_rtt_ms: "RTT", vcu_comms_latency_ms: "Latency", vcu_comms_drop_pct: "Drop", vcu_cpu_temp_c: "CPU temp",
    gnss_mode: "Fix", gnss_corr_age_s: "Corr. age", gnss_sats: "Sats", rajant_peers: "Peers", rajant_temp_c: "Temp",
    router_cpu_pct: "CPU", router_temp_c: "Temp", power_soc_pct: "Battery", power_pv_w: "Solar", power_load_w: "Load",
    host_cpu_pct: "CPU", host_mem_used_pct: "Mem", client_connected: "Session", mobius_clients: "Clients", mobius_assets: "Assets" };
  return map[metric] || fmtLabel(metric);
}
function fmtLabel(m) {
  return (store.meta?.catalog?.metrics?.[m]?.label) || m;
}

// ---------------------------------------------------------------------------
// Charts for a set of components. Live: last hour, refreshed every 30 s. History: the range.
export function chartsSection(items, { primary = true, title = "Charts" } = {}) {
  const grid = h("div", { class: "charts" });
  const hint = h("span", { class: "hint" });
  const el = h("section", { class: "section" }, h("h2", {}, title, hint), grid);
  const cards = new Map();
  let timer = null;
  let gen = 0;
  async function load() {
    const my = ++gen;
    const w = window_();
    setText(hint, rangeLabel());
    for (const c of cards.values()) c.loading(true);
    await Promise.all(items.map(async ({ comp, label }) => {
      let charts;
      try {
        charts = await get(`/api/history/charts/${comp}`, { from: w.from, to: w.to, primary: primary ? "true" : "" });
      } catch (e) {
        console.error(e);
        return;
      }
      if (my !== gen) return;
      for (const ch of charts) {
        const key = `${comp}:${ch.key}`;
        let card = cards.get(key);
        if (!card) {
          card = chartCard({ title: ch.title, sub: label });
          cards.set(key, card);
        }
        card.set(ch);
      }
    }));
    if (my !== gen) return;
    // keep the inventory order
    clear(grid);
    for (const { comp } of items) for (const [k, c] of cards) if (k.startsWith(comp + ":")) grid.append(c.el);
    if (!grid.children.length) grid.append(h("p", { class: "muted", text: "No charts for these components." }));
  }
  function start() {
    stop();
    load();
    if (window_().live) timer = setInterval(load, 30000);
  }
  function stop() { if (timer) clearInterval(timer); timer = null; }
  return { el, start, stop, destroy() { stop(); for (const c of cards.values()) c.destroy(); } };
}

// ---------------------------------------------------------------------------
export function eventsSection(nodeId, nav) {
  const list = h("ul", { class: "panel", style: "list-style:none;margin:0;padding:0" });
  const el = h("section", { class: "section" }, h("h2", { text: "Recent changes" }), list);
  async function load() {
    const w = window_();
    const from = w.live ? Date.now() / 1000 - 86400 : w.from;
    const rows = await get("/api/events", { node: nodeId, from, to: w.to, limit: 40, info: "true" });
    clear(list);
    if (!rows.length) list.append(h("li", { class: "muted", style: "padding:10px 14px", text: "No changes in this period." }));
    for (const e of rows) list.append(h("li", { style: "border-bottom:1px solid var(--hair)" }, eventLink(e, nav)));
  }
  return { el, load };
}

const SYS_LABEL = { update: "Update", report: "Report", admin: "Admin" };

export function eventLink(e, nav) {
  if (e.kind === "system") {            // updates, morning reports, sign-ins: nothing to open
    const chip = e.level >= 3 ? badge(e.level, { small: true }) : h("span", { class: "chip info", text: SYS_LABEL[e.sys_kind] || "System" });
    return h("div", { style: "display:block;padding:8px 14px" },
      h("div", { class: "ev-row1" }, h("span", { class: "t", text: fmtWhen(e.ts) }), chip,
        h("span", { class: "n", text: e.actor ? `${SYS_LABEL[e.sys_kind] || "System"} · ${e.actor}` : SYS_LABEL[e.sys_kind] || "System" })),
      h("div", { class: "ev-why", text: e.reason }));
  }
  const target = e.node.startsWith && store.comps[e.node] ? `#/c/${e.node}` : e.group ? `#/${e.tab}/${e.group}` : `#/${e.tab}`;
  const from = Math.floor(e.ts - 3600), to = Math.ceil(e.ts + 3600);
  const change = e.kind === "info"
    ? [h("span", { class: "chip info", text: e.key === "address" ? "Address changed" : `${e.key} changed` })]
    : [badge(e.old ?? 1, { small: true }), "→", badge(e.new, { small: true })];
  const a = h("a", { href: `${target}?mode=history&from=${from}&to=${to}`, style: "display:block;padding:8px 14px;color:var(--ink);text-decoration:none" },
    h("div", { class: "ev-row1" }, h("span", { class: "t", text: fmtWhen(e.ts) }), ...change, h("span", { class: "n", text: e.name })),
    e.reason ? h("div", { class: "ev-why", text: e.reason }) : null);
  a.addEventListener("click", (ev) => { ev.preventDefault(); nav(a.getAttribute("href")); });
  return a;
}

export function statusLabel(st) { return STATUS_LABEL[statusName(st)]; }
