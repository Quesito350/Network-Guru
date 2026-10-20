// Vehicle path: Mobius Server -> site network -> Rajant mesh / Celona -> radio -> router -> VCU -> VAI.

import { h, s, setClass, setText } from "../dom.js";
import { fmtPct, fmtValue, metricLevel, statusName } from "../fmt.js";
import { node, store } from "../store.js";

const GLYPH = {
  server: "M3 4h14v9H3zM7 16h6M10 13v3",
  core: "M3 7h14v6H3zM6 10h.01M9 10h.01M12 10h.01",
  access: "M10 17V9M6.5 6.5a5 5 0 0 1 7 0M4 4a8.5 8.5 0 0 1 12 0M10 9l-3 8M10 9l3 8",
  radio: "M10 17V8M7 5.5a4 4 0 0 1 6 0M5 3.5a7 7 0 0 1 10 0",
  router: "M3 10h14v5H3zM6 12.5h.01M9 12.5h.01M7 10l-2-5M13 10l2-5",
  vcu: "M6 6h8v8H6zM8 3v3M12 3v3M8 14v3M12 14v3M3 8h3M3 12h3M14 8h3M14 12h3",
  vai: "M2 10s3-5 8-5 8 5 8 5-3 5-8 5-8-5-8-5zM10 12.2a2.2 2.2 0 1 0 0-4.4 2.2 2.2 0 0 0 0 4.4z",
};
const EDGE_LABEL = { ping_rtt_ms: "", vcu_comms_latency_ms: "Mobius", vai_heartbeat_age_s: "heartbeat" };

function glyph(key) {
  return s("svg", { viewBox: "0 0 20 20", "aria-hidden": "true" },
    s("path", { d: GLYPH[key] || GLYPH.core, fill: "none", stroke: "var(--ink-2)", "stroke-width": 1.6, "stroke-linecap": "round", "stroke-linejoin": "round" }));
}

function accessState(comps) {
  const sts = comps.map((c) => node(c).s);
  const up = sts.filter((x) => x === 0 || x === 3).length;
  let st = 0;
  if (!comps.length) st = 1;
  else if (up === 0) st = 4;
  else if (sts.some((x) => x >= 3) || up < comps.length) st = 3;
  return { st, text: `${up}/${comps.length} up` };
}

export function pathWidget(hops, onOpen) {
  const row = h("div", { class: "path" });
  const parts = [];
  hops.forEach((hop, i) => {
    if (i > 0) {
      const el = h("div", { class: "edge" }, h("div", { class: "el" }), h("div", { class: "line" }));
      row.append(el);
      parts.push({ type: "edge", hop, el, lbl: el.firstChild });
    }
    const ring = h("div", { class: "ring" }, glyph(hop.key));
    const hl = h("div", { class: "hl", text: hop.label });
    const hs = h("div", { class: "hs" });
    const el = h("div", { class: "hop", tabindex: 0, role: "link" }, ring, hl, hs);
    const target = hop.comp ? `#/c/${hop.comp}` : hop.group ? `#/${store.groups[hop.group]?.tab}/${hop.group}` : "#/infrastructure";
    el.addEventListener("click", () => onOpen(target));
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") onOpen(target); });
    row.append(el);
    parts.push({ type: "hop", hop, el, hs });
  });
  const note = h("div", { class: "path-note" });
  const wrap = h("div", { class: "panel" }, row, note);

  function live() {
    setText(note, "Latency on each link is measured from the dashboard server (ping) or by Mobius (VCU comms latency).");
    for (const p of parts) {
      const hop = p.hop;
      if (p.type === "hop") {
        let st, sub;
        if (hop.comps) ({ st, text: sub } = accessState(hop.comps));
        else {
          const n = node(hop.comp || hop.group);
          st = n.s;
          const c = hop.comp ? store.comps[hop.comp] : null;
          sub = n.s === 0 ? (c?.host || "OK") : (n.r?.[0] || "");
        }
        setClass(p.el, "s-", statusName(st));
        p.el.title = sub;
        setText(p.hs, sub.length > 38 ? sub.slice(0, 36) + "…" : sub);
      } else {
        const n = hop.comp ? node(hop.comp) : null;
        const v = n && hop.edge ? n.m?.[hop.edge] : null;
        const lvl = v == null ? null : metricLevel(hop.edge, v) || "ok";
        setClass(p.el, "s-", lvl);
        setText(p.lbl, v == null ? "" : `${EDGE_LABEL[hop.edge] ? EDGE_LABEL[hop.edge] + " " : ""}${fmtValue(hop.edge, v)}`);
      }
    }
  }

  function history(hist) {
    setText(note, "History: each box shows availability over the range; each link shows its 95th-percentile latency.");
    const byKey = Object.fromEntries(hist.map((x) => [x.key, x]));
    for (const p of parts) {
      const hh = byKey[p.hop.key] || {};
      if (p.type === "hop") {
        const a = hh.availability;
        const st = a == null ? 1 : a >= 99 ? 0 : a >= 95 ? 3 : 4;
        setClass(p.el, "s-", statusName(st));
        setText(p.hs, a == null ? "no data" : `${fmtPct(a)} available`);
      } else {
        const v = hh.edge_p95;
        const lvl = v == null ? null : metricLevel(p.hop.edge, v) || "ok";
        setClass(p.el, "s-", lvl);
        setText(p.lbl, v == null ? "" : `p95 ${fmtValue(p.hop.edge, v)}`);
      }
    }
  }

  return { el: wrap, live, history };
}

export function miniPath(hops) {
  const row = h("div", { class: "minipath", "aria-label": "Path to the vehicle" });
  const dots = [];
  hops.forEach((hop, i) => {
    if (i) row.append(h("span", { class: "mp-link" }));
    const d = h("span", { class: "sdot" });
    row.append(h("span", { class: "mp-node" }, d, h("span", { text: { server: "Server", core: "Site", access: hop.label.startsWith("Celona") ? "LTE" : "Mesh", radio: "Radio", router: "Router", vcu: "VCU", vai: "VAI" }[hop.key] || hop.key })));
    dots.push({ hop, d });
  });
  function update() {
    for (const { hop, d } of dots) {
      const st = hop.comps ? accessState(hop.comps).st : node(hop.comp || hop.group).s;
      setClass(d, "s-", statusName(st));
      d.title = hop.label;
    }
  }
  return { el: row, update };
}
