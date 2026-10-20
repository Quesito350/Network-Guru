// Small pieces: sparkline, status strip, battery meter.

import { h, s } from "../dom.js";
import { fmtDuration, fmtTime, fmtValue, statusName, STATUS_LABEL } from "../fmt.js";
import { hideTip, showTip } from "./chart.js";

const FILL = { ok: "var(--s-ok)", warning: "var(--s-warning)", critical: "var(--s-critical)", offline: "var(--s-offline)", unknown: "var(--s-unknown)" };

export function sparkline(points, { from, to, metric, width = 200, height = 28 } = {}) {
  const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", class: "spark", "aria-hidden": "true" });
  if (!points || points.length < 2) return svg;
  const vs = points.map((p) => p[1]);
  let lo = Math.min(...vs), hi = Math.max(...vs);
  if (lo === hi) { lo -= 1; hi += 1; }
  const t0 = from ?? points[0][0], t1 = to ?? points[points.length - 1][0];
  const x = (t) => 2 + ((t - t0) / (t1 - t0)) * (width - 8);
  const y = (v) => 3 + (1 - (v - lo) / (hi - lo)) * (height - 6);
  const d = points.map(([t, v], i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join("");
  svg.append(s("path", { d, fill: "none", stroke: "var(--series-1)", "stroke-width": 1.6, "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke" }));
  const [lt, lv] = points[points.length - 1];
  svg.append(s("circle", { cx: x(lt), cy: y(lv), r: 2.6, fill: "var(--series-1)" }));
  return h("span", { class: "spark-wrap", title: metric ? `${fmtValue(metric, lv)} latest · range ${fmtValue(metric, Math.min(...vs))} – ${fmtValue(metric, Math.max(...vs))}` : "" }, svg);
}

// segments: [[t0, t1, status], ...]
export function statusStrip(segments, from, to, { height = 10 } = {}) {
  const W = 300;
  const svg = s("svg", { viewBox: `0 0 ${W} ${height}`, preserveAspectRatio: "none", class: "strip", role: "img", "aria-label": "Status over the selected range" });
  const x = (t) => ((t - from) / (to - from)) * W;
  for (const [a, b, st] of segments || []) {
    const name = statusName(st);
    const r = s("rect", { x: x(a), y: 0, width: Math.max(0.6, x(b) - x(a)), height, fill: FILL[name] });
    r.addEventListener("pointermove", (ev) => showTip(ev.clientX, ev.clientY, `${fmtTime(a, "full")} – ${fmtTime(b, "full")}`,
      [{ key: FILL[name], box: true, value: STATUS_LABEL[name], label: fmtDuration(b - a) }]));
    r.addEventListener("pointerleave", hideTip);
    svg.append(r);
  }
  return svg;
}

export function socLevel(soc) {
  if (soc == null) return "unknown";
  return soc <= 20 ? "critical" : soc <= 40 ? "warning" : "ok";
}

export function battery(soc, { big = false } = {}) {
  const lvl = socLevel(soc);
  const fill = h("div", { class: `batt-fill s-${lvl}` });
  fill.style.width = `${Math.max(2, Math.min(100, soc ?? 0))}%`;
  return h("div", { class: `batt${big ? " big" : ""}`, title: "Battery state of charge" },
    h("div", { class: "batt-shell" }, fill), h("span", { class: "pct num", text: soc == null ? "–" : `${Math.round(soc)}%` }));
}
