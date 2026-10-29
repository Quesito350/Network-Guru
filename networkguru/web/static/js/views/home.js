// Overview (0.9.3, the start page): a panel each for Vehicles, Control Room and Infrastructure - the tab's
// status, counts, a few headline numbers and a chip per vehicle / station / location (worst first) - then
// one "Needs attention now" list for all three. In Live, groups that have gone quiet are left out as on
// the tabs (quiet.js); History colours the chips by the worst status of the range and hides the numbers
// (they are live).

import { get } from "../api.js";
import { h, setClass, setText } from "../dom.js";
import { fmtValue, metricLevel, statusName, STATUS_LABEL } from "../fmt.js";
import { isQuiet, quietText, setShowHidden, showHidden } from "../quiet.js";
import { badge, countsLine, dot, updateBadge, updateDot } from "../status.js";
import { node, rangeLabel, store, window_ } from "../store.js";
import { attentionRows, fillAttention, TAB_TITLE } from "./common.js";

const TABS = ["vehicles", "control", "infrastructure"];
const NOUN = { vehicles: ["vehicle", "vehicles"], control: ["station", "stations"], infrastructure: ["location", "locations"] };
const ANSWERING = (s) => s !== 1 && s !== 2;          // not Unknown, not Offline

const compsOf = (type) => Object.values(store.comps).filter((c) => c.type === type);
const metric = (cid, m) => (cid ? node(cid).m?.[m] ?? null : null);
const firstMetric = (type, m) => compsOf(type).map((c) => metric(c.id, m)).find((v) => v != null) ?? null;
const count = (v) => (v == null ? { text: "–" } : { text: String(Math.round(v)) });

function measured(m, v, sub = "") {
  return v == null ? { text: "–" } : { text: fmtValue(m, v), level: metricLevel(m, v), sub };
}

function worstLatency(groups) {
  let best = null;
  for (const g of groups) {
    const vcu = g.components.find((c) => c.type === "vcu");
    const radio = g.components.find((c) => c.type === "rajant" || c.type === "cell_modem");
    const [m, v] = metric(vcu?.id, "vcu_comms_latency_ms") != null ? ["vcu_comms_latency_ms", metric(vcu.id, "vcu_comms_latency_ms")]
      : ["ping_rtt_ms", metric(radio?.id, "ping_rtt_ms")];
    if (v != null && (!best || v > best.v)) best = { m, v, g };
  }
  return best ? measured(best.m, best.v, best.g.name) : { text: "–" };
}

function lowestBattery() {
  let low = null;
  for (const c of compsOf("victron_cerbo")) {
    const v = metric(c.id, "power_soc_pct");
    if (v != null && (!low || v < low.v)) low = { v, c };
  }
  return low ? measured("power_soc_pct", low.v, store.groups[low.c.group]?.name || "") : { text: "–" };
}

function linksDown() {
  const seen = new Set();
  let pairs = 0, down = 0;
  for (const c of Object.values(store.comps)) {
    if (!c.partner || !store.comps[c.partner]) continue;
    const key = [c.id, c.partner].sort().join("|");
    if (seen.has(key)) continue;
    seen.add(key);
    pairs += 1;
    if ([c.id, c.partner].some((id) => node(id).s === 2 || node(id).s === 4)) down += 1;
  }
  return pairs ? { text: `${down} of ${pairs}`, level: down ? "critical" : null } : { text: "–" };
}

const answering = (groups) => ({ text: `${groups.filter((g) => ANSWERING(node(g.id).s)).length} of ${groups.length}` });

const NUMBERS = {
  vehicles: [
    { label: "In service", value: answering },
    { label: "Talking to Mobius", value: () => count(firstMetric("mobius_server", "mobius_assets")) },
    { label: "Worst comms latency", value: worstLatency },
  ],
  control: [
    { label: "Mobius assets", value: () => count(firstMetric("mobius_server", "mobius_assets")) },
    { label: "Mobius clients", value: () => count(firstMetric("mobius_server", "mobius_clients")) },
    { label: "InfluxDB data age", value: () => measured("influx_write_age_s", firstMetric("influxdb", "influx_write_age_s")) },
  ],
  infrastructure: [
    { label: "Locations answering", value: answering },
    { label: "Lowest battery", value: lowestBattery },
    { label: "60 GHz links down", value: linksDown },
  ],
};

function numberCell(spec) {
  const md = h("span", { class: "mdot hidden" });
  const v = h("div", { class: "v num" });
  const sub = h("div", { class: "k home-sub" });
  return {
    el: h("div", {}, h("div", { class: "k" }, spec.label, md), v, sub),
    update(groups) {
      const r = spec.value(groups);
      setText(v, r.text);
      v.classList.toggle("na", r.text === "–");
      setText(sub, r.sub || "");
      setClass(md, "s-", r.level || "");
      md.classList.toggle("hidden", !r.level);
      md.title = r.level ? STATUS_LABEL[r.level] : "";
    },
  };
}

function panel(tabId, nav) {
  const tab = store.inv.tabs.find((t) => t.id === tabId);
  const groups = (tab?.groups || []).map((g) => store.groups[g.id]);
  const b = badge(1, { small: true });
  const go = (href) => (e) => { e.preventDefault(); nav(href); };
  const head = h("a", { class: "home-head", href: `#/${tabId}`, onclick: go(`#/${tabId}`) },
    h("h2", { text: TAB_TITLE[tabId] }), b);
  const counts = h("div", { class: "home-counts" });
  const cells = NUMBERS[tabId].map(numberCell);
  const nums = h("div", { class: "kv home-nums" }, cells.map((c) => c.el));
  const liveNote = h("p", { class: "muted small hidden", text: "Numbers are live - History shows the worst status of the range." });
  const chipsBox = h("div", { class: "home-chips" });
  const more = h("p", { class: "muted small home-more hidden" });
  const chips = {};
  for (const g of groups) {
    const d = dot(1);
    const a = h("a", { class: "chip", href: `#/${tabId}/${g.id}`, onclick: go(`#/${tabId}/${g.id}`) }, d, h("span", { text: g.name }));
    chips[g.id] = { a, d };
  }
  const el = h("section", { class: "panel home-panel" }, head, counts, nums, liveNote, chipsBox, more);
  return { tabId, groups, el, b, counts, cells, nums, liveNote, chipsBox, more, chips, order: "" };
}

export function homeView(root, nav) {
  const panels = TABS.filter((t) => store.inv.tabs.some((x) => x.id === t)).map((t) => panel(t, nav));
  const all = panels.flatMap((p) => p.groups);
  const sub = h("span", { class: "sub" });
  const quietBar = h("div", { class: "quiet-bar hidden" });
  const issues = h("ul");
  root.append(
    h("div", { class: "page-head" }, h("h1", { text: "Overview" }), sub),
    h("div", { class: "home-panels" }, panels.map((p) => p.el)),
    quietBar,
    h("section", { class: "issues" }, h("h2", { text: "Needs attention now" }), issues));

  let rangeWorst = null;              // History: worst status per group over the range

  function update() {
    const live = !rangeWorst;
    const hide = !showHidden("overview");
    const quiet = new Set(all.filter((g) => isQuiet(g)).map((g) => g.id));
    const hidden = hide ? quiet : new Set();
    for (const p of panels) {
      const st = (g) => (rangeWorst ? rangeWorst[g.id] ?? 1 : node(g.id).s);
      const shown = p.groups.filter((g) => !hidden.has(g.id));
      const c = {};
      for (const g of shown) c[statusName(st(g))] = (c[statusName(st(g))] || 0) + 1;
      updateBadge(p.b, live ? node(p.tabId).s : shown.length ? Math.max(...shown.map(st)) : 1);
      p.counts.replaceChildren(...(shown.length ? countsLine(c) : [h("span", { class: "muted", text: `No ${NOUN[p.tabId][1]}` })]));
      p.nums.classList.toggle("hidden", !live);
      p.liveNote.classList.toggle("hidden", live);
      if (live) p.cells.forEach((x) => x.update(p.groups));
      for (const g of p.groups) {
        const { a, d } = p.chips[g.id];
        const name = statusName(st(g));
        setClass(a, "s-", name);
        a.classList.toggle("quiet", quiet.has(g.id));
        updateDot(d, st(g), `${g.name}: ${STATUS_LABEL[name]}`);
        a.title = quiet.has(g.id) ? `${g.name} - ${quietText(g)}` : `${g.name}: ${(node(g.id).r || [])[0] || STATUS_LABEL[name]}`;
      }
      const order = [...shown].sort((x, y) => st(y) - st(x) || x.name.localeCompare(y.name));
      const key = order.map((g) => g.id).join(",");
      if (key !== p.order) {                       // re-order only when it changes (keeps focus and hover)
        p.order = key;
        p.chipsBox.replaceChildren(...order.map((g) => p.chips[g.id].a));
      }
      const away = p.groups.filter((g) => hidden.has(g.id));
      const worstAway = away.length ? Math.max(...away.map(st)) : 0;
      p.more.classList.toggle("hidden", !away.length);
      setText(p.more, away.length ? `+${away.length} hidden - gone quiet`
        + (worstAway >= 3 ? ` (${STATUS_LABEL[statusName(worstAway)]} among them)` : "") : "");
    }
    setText(sub, `${store.inv.site.name} · ${live ? "status now" : `worst status during the ${rangeLabel()}`}`);
    quietBar.classList.toggle("hidden", !quiet.size);
    if (quiet.size) {
      const mins = store.meta.hide_quiet?.min || 60;
      const what = `${quiet.size} with no answer for over ${mins >= 120 ? `${Math.round(mins / 60)} hours` : `${mins} minute${mins === 1 ? "" : "s"}`}`;
      quietBar.replaceChildren(h("span", { text: hide ? `${what} - hidden from Live.` : `${what} - shown (faded).` }),
        h("button", { type: "button", class: "linkish", text: hide ? "Show them" : "Hide them",
          onclick: () => { setShowHidden("overview", hide); update(); } }));
    }
    fillAttention(issues, attentionRows(all, hidden), nav);
  }

  let timer = null;
  let gen = 0;
  async function loadHistory() {
    const my = ++gen;
    const w = window_();
    if (w.live) {
      rangeWorst = null;
      update();
      return;
    }
    let st;
    try { st = await get("/api/history/status", { ids: all.map((g) => g.id).join(","), from: w.from, to: w.to }); } catch (e) { console.error(e); return; }
    if (my !== gen) return;
    rangeWorst = {};
    for (const g of all) {
      const segs = st[g.id] || [];
      const inService = segs.filter((x) => x[2] !== 1 && x[2] !== 2).map((x) => x[2]);
      rangeWorst[g.id] = inService.length ? Math.max(...inService) : segs.some((x) => x[2] === 2) ? 2 : 1;
    }
    update();
  }

  update();
  loadHistory();
  if (!window_().live) timer = setInterval(loadHistory, 60000);
  return {
    update,
    refreshRange() { loadHistory(); },
    destroy() { if (timer) clearInterval(timer); },
  };
}
