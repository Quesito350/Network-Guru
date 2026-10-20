// SVG charts: line / area (with crosshair tooltip), state timelines and daily bars.
// Thin marks, hairline grid, one y-axis, legend for 2+ series, table view on every chart.

import { clear, h, s } from "../dom.js";
import { bpsScale, fmtTime, fmtValue, metricSpec, stateLevel } from "../fmt.js";

const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];
const STATUS_FILL = { ok: "var(--s-ok)", warning: "var(--s-warning)", critical: "var(--s-critical)", offline: "var(--s-offline)", unknown: "var(--s-unknown)" };
// Charger states are identities, not health (only Fault is bad).
const MPPT = { 0: "var(--s-offline)", 2: "var(--s-critical)", 3: "var(--series-1)", 4: "var(--series-3)", 5: "var(--series-2)", 6: "var(--series-4)", 7: "var(--series-4)" };
const H_PLOT = 150;
const PAD = { l: 46, r: 12, t: 10, b: 24 };

let tipEl = null;
function tip() {
  if (!tipEl) {
    tipEl = h("div", { class: "tip hidden", role: "tooltip" });
    document.body.append(tipEl);
  }
  return tipEl;
}
export function showTip(x, y, title, rows) {
  const el = tip();
  el.replaceChildren(h("div", { class: "tt", text: title }),
    ...rows.map((r) => h("div", { class: "tr" },
      r.key ? h("span", { class: r.box ? "key-box" : "key-line", style: `background:${r.key}` }) : null,
      h("b", { text: r.value }), r.label ? h("span", { class: "lbl", text: r.label }) : null)));
  el.classList.remove("hidden");
  const w = el.offsetWidth, hh = el.offsetHeight;
  let left = x + 14, top = y + 14;
  if (left + w > window.innerWidth - 8) left = x - w - 14;
  if (top + hh > window.innerHeight - 8) top = y - hh - 14;
  el.style.left = `${Math.max(8, left)}px`;
  el.style.top = `${Math.max(8, top)}px`;
}
export function hideTip() { tipEl?.classList.add("hidden"); }

function niceStep(span, n) {
  const raw = span / Math.max(1, n);
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= raw) return m * p;
  return 10 * p;
}

function yDomain(lo, hi, ymin, ymax, integer = false) {
  if (ymin != null) lo = Math.min(lo, ymin);
  if (ymax != null) hi = Math.max(hi, ymax);
  if (lo === hi) { const d = Math.abs(lo) * 0.1 || 1; lo -= d; hi += d; }
  let step = niceStep(hi - lo, 4);
  if (integer) step = Math.max(1, Math.round(step));        // counts: whole numbers only
  const a = ymin != null ? ymin : Math.floor(lo / step) * step;
  const b = ymax != null ? ymax : Math.ceil(hi / step) * step;
  const ticks = [];
  for (let v = Math.ceil(a / step) * step; v <= b + step * 1e-6; v += step) ticks.push(+v.toFixed(10));
  const dec = Math.max(0, Math.ceil(-Math.log10(step) - 1e-9) + (String(step).includes("25") ? 1 : 0));
  return { lo: a, hi: b, ticks, dec: Math.min(dec, 4) };
}

const TIME_STEPS = [300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
function xTicks(from, to, width) {
  const n = Math.max(2, Math.floor(width / 90));
  const step = TIME_STEPS.find((st) => (to - from) / st <= n) || 604800;
  const out = [];
  // align to the site's local clock by shifting with the local offset of `from`
  const off = localOffset(from);
  for (let t = Math.ceil((from + off) / step) * step - off; t <= to; t += step) out.push(t);
  return { ticks: out, step };
}
function localOffset(ts) {
  // seconds to add to UTC epoch to get site-local wall time
  const hm = fmtTime(ts, "hm").split(":").map(Number);
  const utc = new Date(ts * 1000);
  let off = (hm[0] * 60 + hm[1] - (utc.getUTCHours() * 60 + utc.getUTCMinutes())) * 60;
  if (off > 43200) off -= 86400;
  if (off < -43200) off += 86400;
  return off;
}
function xLabel(t, step) {
  if (step >= 86400) return fmtTime(t, "date");
  const hm = fmtTime(t);
  return hm === "00:00" ? fmtTime(t, "date") : hm;
}

export function chartCard({ title, sub } = {}) {
  const legend = h("div", { class: "legend" });
  const tbtn = h("button", { class: "tbtn", type: "button", "aria-pressed": "false", text: "Table" });
  const body = h("div", { class: "cbody" });
  const el = h("figure", { class: "chart loading" },
    h("figcaption", {}, h("span", { class: "ct", text: title || "" }), sub ? h("span", { class: "cs", text: sub }) : null, legend, tbtn),
    body);
  const state = { data: null, table: false, width: 0 };
  const render = () => {
    if (!state.data) return;
    state.width = body.clientWidth;
    clear(body);
    clear(legend);
    if (state.table) return renderTable(body, state.data);
    const d = state.data;
    if (d.kind === "state") drawState(body, legend, d);
    else if (d.kind === "daily") drawDaily(body, legend, d);
    else drawLines(body, legend, d);
  };
  tbtn.addEventListener("click", () => {
    state.table = !state.table;
    tbtn.setAttribute("aria-pressed", String(state.table));
    tbtn.textContent = state.table ? "Chart" : "Table";
    render();
  });
  const ro = new ResizeObserver(() => {
    if (Math.abs(body.clientWidth - state.width) > 4) render();
  });
  ro.observe(body);
  return {
    el,
    set(data) {
      state.data = data;
      el.classList.remove("loading");
      render();
    },
    loading(on) { el.classList.toggle("loading", on); },
    destroy() { ro.disconnect(); },
  };
}

function seriesScale(d) {
  const unit = metricSpec(d.series[0]?.metric || "").unit;
  if (unit === "bps") {
    let mx = 0;
    for (const se of d.series) for (const [, v] of se.peak || se.points) mx = Math.max(mx, Math.abs(v));
    const sc = bpsScale(mx);
    return { div: sc.div, unit: sc.unit };
  }
  return { div: 1, unit };
}

function fmtAxis(v, unit, dec = 0) {
  const a = Math.abs(v);
  let txt = a >= 10000 && dec === 0 ? (v / 1000).toFixed(0) + "k" : v.toFixed(dec);
  if (unit === "%") txt += "%";
  return txt;
}

// A break in the line = no sample for 2.5x the normal spacing (the step, or the data's own
// spacing when samples are sparser than the step, e.g. older, thinned-out history).
function gapFor(seriesList, step) {
  const diffs = [];
  for (const se of seriesList) for (let i = 1; i < se.points.length; i++) diffs.push(se.points[i][0] - se.points[i - 1][0]);
  diffs.sort((a, b) => a - b);
  const median = diffs.length ? diffs[diffs.length >> 1] : step;
  return 2.5 * Math.max(step || 60, median);
}

function emptyState(body, text) {
  body.append(h("div", { class: "empty", text }));
}

function drawLines(body, legend, d) {
  const series = d.series.filter((x) => x.points.length);
  if (!series.length) return emptyState(body, "No data in this range");
  const W = Math.max(240, body.clientWidth || 400);
  const H = H_PLOT + PAD.t + PAD.b;
  const sc = seriesScale(d);
  let lo = Infinity, hi = -Infinity;
  for (const se of series) for (const [, v] of [...se.points, ...(se.peak || [])]) { lo = Math.min(lo, v / sc.div); hi = Math.max(hi, v / sc.div); }
  // thresholds near the data join the domain so the lines are visible when they matter
  const thr = [];
  for (const [metric, t] of Object.entries(d.thresholds || {})) {
    if (sc.div !== 1) continue;
    for (const [k, lvl] of [["crit", "critical"], ["warn", "warning"], ["low_crit", "critical"], ["low_warn", "warning"]]) {
      if (t[k] == null) continue;
      const span = Math.max(hi - lo, Math.abs(hi) * 0.5, 1);
      if (t[k] >= lo - span && t[k] <= hi + span) thr.push({ v: t[k], lvl, metric });
    }
  }
  for (const t of thr) { lo = Math.min(lo, t.v); hi = Math.max(hi, t.v); }
  const ymin = d.ymin ?? (lo >= 0 && d.kind === "area" ? 0 : null);
  const counts = series.every((se) => { const sp = metricSpec(se.metric); return !sp.unit && sp.dec === 0; });
  const dom = yDomain(lo, hi, ymin, d.ymax, counts);
  const x = (t) => PAD.l + ((t - d.start) / (d.end - d.start)) * (W - PAD.l - PAD.r);
  const y = (v) => PAD.t + (1 - (v - dom.lo) / (dom.hi - dom.lo)) * H_PLOT;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": d.title });
  const ax = s("g", { class: "ax" });
  for (const v of dom.ticks) {
    ax.append(s("line", { x1: PAD.l, x2: W - PAD.r, y1: y(v), y2: y(v), class: v === dom.lo ? "base" : "" }));
    ax.append(s("text", { x: PAD.l - 6, y: y(v) + 4, "text-anchor": "end" }, fmtAxis(v, sc.unit, dom.dec)));
  }
  const xt = xTicks(d.start, d.end, W - PAD.l - PAD.r);
  for (const t of xt.ticks) ax.append(s("text", { x: x(t), y: H - 6, "text-anchor": "middle" }, xLabel(t, xt.step)));
  if (sc.unit && sc.unit !== "%") ax.append(s("text", { x: 2, y: PAD.t - 1, "text-anchor": "start" }, sc.unit));
  svg.append(ax);
  const gapAfter = gapFor(series, d.step);
  const pathFor = (pts) => {
    let out = "", prev = null;
    for (const [t, v] of pts) {
      out += `${prev == null || t - prev > gapAfter ? "M" : "L"}${x(t).toFixed(1)},${y(v / sc.div).toFixed(1)}`;
      prev = t;
    }
    return out;
  };
  const runs = (pts) => {
    const out = [];
    let cur = [];
    for (const p of pts) {
      if (cur.length && p[0] - cur[cur.length - 1][0] > gapAfter) { out.push(cur); cur = []; }
      cur.push(p);
    }
    if (cur.length) out.push(cur);
    return out;
  };
  series.forEach((se, i) => {
    const col = SERIES[i % SERIES.length];
    if (se.peak && se.peak.length) {
      const pk = new Map(se.peak.map(([t, v]) => [t, v]));
      for (const run of runs(se.points)) {
        const top = run.map(([t, v]) => [t, Math.max(v, pk.get(t) ?? v)]);
        const dd = top.map(([t, v], j) => `${j ? "L" : "M"}${x(t).toFixed(1)},${y(v / sc.div).toFixed(1)}`).join("")
          + [...run].reverse().map(([t, v]) => `L${x(t).toFixed(1)},${y(v / sc.div).toFixed(1)}`).join("") + "Z";
        svg.append(s("path", { d: dd, fill: col, "fill-opacity": 0.14, stroke: "none" }));
      }
    }
    if (d.kind === "area") {
      const base = y(Math.max(dom.lo, 0));
      for (const run of runs(se.points)) {
        const dd = `M${x(run[0][0]).toFixed(1)},${base}` + run.map(([t, v]) => `L${x(t).toFixed(1)},${y(v / sc.div).toFixed(1)}`).join("")
          + `L${x(run[run.length - 1][0]).toFixed(1)},${base}Z`;
        svg.append(s("path", { d: dd, fill: col, "fill-opacity": 0.10, stroke: "none" }));
      }
    }
    svg.append(s("path", { d: pathFor(se.points), fill: "none", stroke: col, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    for (const run of runs(se.points)) {                // a lone sample would be invisible as a line
      if (run.length === 1) svg.append(s("circle", { cx: x(run[0][0]), cy: y(run[0][1] / sc.div), r: 2.5, fill: col }));
    }
  });
  const tg = s("g", { class: "thr" });
  for (const t of thr) {
    const col = t.lvl === "critical" ? "var(--s-critical)" : "var(--s-warning)";
    tg.append(s("line", { x1: PAD.l, x2: W - PAD.r, y1: y(t.v), y2: y(t.v), stroke: col }));
    tg.append(s("text", { x: W - PAD.r - 2, y: y(t.v) - 3, "text-anchor": "end" }, `${t.lvl === "critical" ? "critical" : "warning"} ${fmtValue(t.metric, t.v)}`));
  }
  svg.append(tg);
  if (series.length > 1 || d.series.length > 1) {
    d.series.forEach((se, i) => legend.append(h("span", {}, h("span", { class: d.kind === "area" ? "key-box" : "key-line", style: `background:${SERIES[i % SERIES.length]}` }), se.label)));
  }
  if (series.some((se) => se.peak)) legend.append(h("span", {}, h("span", { class: "key-box", style: "background:var(--series-1);opacity:.25" }), "peak"));
  // hover layer: crosshair snaps to the nearest sample
  const times = [...new Set(series.flatMap((se) => se.points.map((p) => p[0])))].sort((a, b) => a - b);
  const maps = series.map((se) => ({ pts: new Map(se.points), peak: se.peak ? new Map(se.peak) : null }));
  const cross = s("line", { class: "crosshair hidden", y1: PAD.t, y2: PAD.t + H_PLOT });
  const dots = s("g", {});
  svg.append(cross, dots);
  const hit = s("rect", { x: PAD.l, y: PAD.t, width: W - PAD.l - PAD.r, height: H_PLOT, fill: "transparent", tabindex: 0 });
  const at = (px) => {
    const t = d.start + ((px - PAD.l) / (W - PAD.l - PAD.r)) * (d.end - d.start);
    let lo2 = 0, hi2 = times.length - 1;
    while (hi2 - lo2 > 1) { const m = (lo2 + hi2) >> 1; if (times[m] < t) lo2 = m; else hi2 = m; }
    return Math.abs(times[lo2] - t) <= Math.abs(times[hi2] - t) ? times[lo2] : times[hi2];
  };
  const show = (t, cx, cy) => {
    cross.classList.remove("hidden");
    cross.setAttribute("x1", x(t));
    cross.setAttribute("x2", x(t));
    dots.replaceChildren();
    const rows = [];
    maps.forEach((m, i) => {
      const v = m.pts.get(t);
      const col = SERIES[i % SERIES.length];
      if (v == null) return;
      dots.append(s("circle", { cx: x(t), cy: y(v / sc.div), r: 4, fill: col, stroke: "var(--card)", "stroke-width": 2 }));
      rows.push({ key: col, value: fmtValue(series[i].metric, v), label: series[i].label });
      if (m.peak && m.peak.get(t) != null) rows.push({ value: fmtValue(series[i].metric, m.peak.get(t)), label: "peak" });
    });
    const step = d.step || 60;
    const title = step >= 3600 ? `${fmtTime(t - step, "full")} – ${fmtTime(t)}` : fmtTime(t, step < 60 ? "fulls" : "full");
    showTip(cx, cy, title, rows);
  };
  hit.addEventListener("pointermove", (ev) => {
    const r = svg.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * W;
    if (times.length) show(at(px), ev.clientX, ev.clientY);
  });
  hit.addEventListener("pointerleave", () => { cross.classList.add("hidden"); dots.replaceChildren(); hideTip(); });
  hit.addEventListener("focus", () => {
    if (!times.length) return;
    const r = svg.getBoundingClientRect();
    show(times[times.length - 1], r.right - 40, r.top + 20);
  });
  hit.addEventListener("blur", () => { cross.classList.add("hidden"); hideTip(); });
  svg.append(hit);
  body.append(svg);
}

function stateFill(metric, code, enumMap) {
  if (metric === "power_mppt_state") return MPPT[code] || "var(--s-unknown)";
  const lvl = stateLevel(metric, code);
  if (lvl) return STATUS_FILL[lvl];
  const keys = Object.keys(enumMap || {});
  return SERIES[Math.max(0, keys.indexOf(String(code))) % SERIES.length];
}

function drawState(body, legend, d) {
  const se = d.series[0];
  if (!se || !se.points.length) return emptyState(body, "No data in this range");
  const W = Math.max(240, body.clientWidth || 400);
  const BAND = 26;
  const H = PAD.t + BAND + PAD.b;
  const x = (t) => PAD.l + ((Math.max(d.start, Math.min(d.end, t)) - d.start) / (d.end - d.start)) * (W - PAD.l - PAD.r);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": d.title });
  const step = d.step || 60;
  const gap = gapFor([se], step);         // polls don't line up with the step exactly
  const segs = [];
  for (const [t, v] of se.points) {
    const code = Math.round(v);
    const last = segs[segs.length - 1];
    if (last && t - last.t1 <= gap) {
      if (last.code === code) { last.t1 = t; continue; }
      segs.push({ code, t0: last.t1, t1: t });
    } else segs.push({ code, t0: t - step, t1: t });
  }
  const g = s("g", {});
  const seen = new Map();
  for (const sg of segs) {
    const fill = stateFill(se.metric, sg.code, d.enum);
    seen.set(sg.code, fill);
    const x0 = x(sg.t0), x1 = x(sg.t1);
    const r = s("rect", { x: x0, y: PAD.t, width: Math.max(1, x1 - x0 - 1), height: BAND, rx: 2, fill });
    r.addEventListener("pointermove", (ev) => showTip(ev.clientX, ev.clientY, `${fmtTime(sg.t0, "full")} – ${fmtTime(sg.t1)}`,
      [{ key: fill, box: true, value: fmtValue(se.metric, sg.code), label: `for ${Math.round((sg.t1 - sg.t0) / 60)} min` }]));
    r.addEventListener("pointerleave", hideTip);
    g.append(r);
  }
  svg.append(g);
  const ax = s("g", { class: "ax" });
  const xt = xTicks(d.start, d.end, W - PAD.l - PAD.r);
  for (const t of xt.ticks) ax.append(s("text", { x: x(t), y: H - 6, "text-anchor": "middle" }, xLabel(t, xt.step)));
  svg.append(ax);
  for (const [code, fill] of [...seen.entries()].sort((a, b) => a[0] - b[0])) {
    legend.append(h("span", {}, h("span", { class: "key-box", style: `background:${fill}` }), fmtValue(se.metric, code)));
  }
  body.append(svg);
}

function drawDaily(body, legend, d) {
  const se = d.series[0];
  if (!se || !se.points.length) return emptyState(body, "No data yet");
  const W = Math.max(240, body.clientWidth || 400);
  const H = H_PLOT + PAD.t + PAD.b;
  const days = [];
  for (let t = d.start; t < d.end - 3600; t += 86400) days.push(t);
  const vals = new Map(se.points.map(([t, v]) => [Math.round(t / 3600), v]));
  const valueAt = (t) => vals.get(Math.round(t / 3600)) ?? vals.get(Math.round(t / 3600) + 1) ?? vals.get(Math.round(t / 3600) - 1);
  const mx = Math.max(...se.points.map((p) => p[1]), 0.1);
  const dom = yDomain(0, mx, 0, null);
  const slot = (W - PAD.l - PAD.r) / days.length;
  const bw = Math.min(24, Math.max(3, slot * 0.62));
  const y = (v) => PAD.t + (1 - (v - dom.lo) / (dom.hi - dom.lo)) * H_PLOT;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": d.title });
  const ax = s("g", { class: "ax" });
  for (const v of dom.ticks) {
    ax.append(s("line", { x1: PAD.l, x2: W - PAD.r, y1: y(v), y2: y(v), class: v === 0 ? "base" : "" }));
    ax.append(s("text", { x: PAD.l - 6, y: y(v) + 4, "text-anchor": "end" }, fmtAxis(v, "", dom.dec)));
  }
  ax.append(s("text", { x: 2, y: PAD.t - 1 }, metricSpec(se.metric).unit));
  const every = Math.ceil(days.length / Math.max(2, Math.floor((W - PAD.l) / 70)));
  days.forEach((t, i) => {
    if ((days.length - 1 - i) % every === 0) ax.append(s("text", { x: PAD.l + slot * (i + 0.5), y: H - 6, "text-anchor": "middle" }, fmtTime(t + 43200, "date")));
  });
  svg.append(ax);
  days.forEach((t, i) => {
    const v = valueAt(t);
    if (v == null) return;
    const cx = PAD.l + slot * (i + 0.5);
    const x0 = cx - bw / 2, top = y(v), base = y(0);
    const r = Math.min(4, bw / 2, base - top);
    const dd = `M${x0},${base}V${top + r}Q${x0},${top} ${x0 + r},${top}H${x0 + bw - r}Q${x0 + bw},${top} ${x0 + bw},${top + r}V${base}Z`;
    svg.append(s("path", { d: dd, fill: "var(--series-1)" }));
    const hitR = s("rect", { x: PAD.l + slot * i, y: PAD.t, width: slot, height: H_PLOT, fill: "transparent" });
    hitR.addEventListener("pointermove", (ev) => showTip(ev.clientX, ev.clientY, fmtTime(t + 43200, "day"),
      [{ key: "var(--series-1)", box: true, value: fmtValue(se.metric, v), label: se.label }]));
    hitR.addEventListener("pointerleave", hideTip);
    svg.append(hitR);
  });
  body.append(svg);
}

function renderTable(body, d) {
  const rows = new Map();
  d.series.forEach((se, i) => {
    for (const [t, v] of se.points) {
      if (!rows.has(t)) rows.set(t, []);
      rows.get(t)[i] = v;
    }
  });
  const times = [...rows.keys()].sort((a, b) => b - a);
  const daily = d.kind === "daily";
  const tbl = h("table", {},
    h("thead", {}, h("tr", {}, h("th", { text: daily ? "Day" : "Time" }), ...d.series.map((se) => h("th", { text: se.label })))),
    h("tbody", {}, ...times.map((t) => h("tr", {},
      h("td", { text: daily ? fmtTime(t + 43200, "day") : fmtTime(t, "full") }),
      ...d.series.map((se, i) => h("td", { text: fmtValue(se.metric, rows.get(t)[i]) }))))));
  body.append(times.length ? h("div", { class: "tablewrap" }, tbl) : h("div", { class: "empty", text: "No data in this range" }));
}
