// Mobius data on the group pages (0.8.0): every value the Mobius InfluxDB has for a vehicle, location
// or desk, from the server's Mobius explorer. Shown as it is: no status comes from these (that is the
// Mobius mapping's job). The Mobius Server's page also lists everything Mobius reports, including names
// not in the inventory.
// 0.11.0: every table (not the first 60), each field's last value kept for a week with its age (a parked
// vehicle keeps its telemetry, faded), Fresh / Everything kept, a count, Download CSV, and a click on a
// number charts its history (1 h - 7 d, from the InfluxDB on demand).

import { get } from "../api.js";
import { downloadCsv } from "../csv.js";
import { clear, h, s, setText } from "../dom.js";
import { fmtAgo, fmtTime, fmtWhen } from "../fmt.js";
import { store } from "../store.js";

const STALE_S = 120;
const FRESH_S = 600;
const SPANS = ["1h", "6h", "24h", "7d"];

function fmtV(v) {
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : String(Number(v.toPrecision(9)));   // keeps GPS digits
  return String(v ?? "");
}
const isNum = (v) => typeof v === "number";
const keyOf = (r) => `${r.m}\u0000${r.f}`;

function link(text, href, nav) {
  const a = h("a", { href, text });
  a.addEventListener("click", (e) => { e.preventDefault(); nav(href); });
  return a;
}

function matches(rows, filter) {
  const f = filter.trim().toLowerCase();
  return f ? rows.filter((r) => r.f.toLowerCase().includes(f) || r.m.toLowerCase().includes(f) || fmtV(r.v).toLowerCase().includes(f)) : rows;
}

// opts.onValue(row): a number was clicked; opts.open: the key of the row whose chart is open; opts.chart: its element
function valuesTable(rows, filter, now, opts = {}) {
  const shown = matches(rows, filter);
  const body = [];
  let last = null;
  for (const r of shown) {
    if (r.m !== last) {
      body.push(h("tr", { class: "mob-meas" }, h("th", { colspan: "3", text: r.m })));
      last = r.m;
    }
    const clickable = opts.onValue && isNum(r.v);
    const val = clickable
      ? h("button", { type: "button", class: "mob-val", title: "Chart its history", "aria-expanded": String(opts.open === keyOf(r)),
        onclick: () => opts.onValue(r) }, h("b", { text: fmtV(r.v) }))
      : h("b", { text: fmtV(r.v) });
    body.push(h("tr", { class: now - r.t > STALE_S ? "stale" : "" }, h("td", { text: r.f }),
      h("td", { class: "num" }, val), h("td", { class: "muted", text: fmtAgo(now - r.t) })));
    if (opts.open === keyOf(r) && opts.chart) body.push(h("tr", { class: "mob-chart-row" }, h("td", { colspan: "3" }, opts.chart)));
  }
  return { table: h("table", { class: "info-table mob-table" }, h("tbody", {}, body)), shown: shown.length };
}

// A small line chart of [[t, v], ...]: no library, the newest on the right, min / max / last labelled.
function spark(points) {
  if (!points.length) return h("p", { class: "muted", text: "No points in this span." });
  const W = 600, H = 110, P = 6;
  const ts = points.map((p) => p[0]), vs = points.map((p) => p[1]);
  const t0 = Math.min(...ts), t1 = Math.max(...ts), lo = Math.min(...vs), hi = Math.max(...vs);
  const x = (t) => P + ((t - t0) / Math.max(1, t1 - t0)) * (W - 2 * P);
  const y = (v) => H - P - ((v - lo) / (hi - lo || 1)) * (H - 2 * P);
  const one = points.length === 1;            // a single point: a flat line across, with the point marked
  const d = one ? `M${P},${H / 2}L${W - P},${H / 2}`
    : points.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  return h("div", { class: "mob-spark" },
    s("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img", "aria-label": "History chart" },
      s("path", { d, fill: "none", stroke: "var(--asi-blue)", "stroke-width": 1.6, "vector-effect": "non-scaling-stroke",
        "stroke-dasharray": one ? "4 4" : null }),
      one ? s("circle", { cx: W - P, cy: H / 2, r: 3, fill: "var(--asi-blue)" }) : null),
    h("div", { class: "mob-spark-labels muted small" },
      h("span", { text: fmtTime(t0, "full") }),
      h("span", { text: `min ${fmtV(lo)} · max ${fmtV(hi)} · last ${fmtV(vs[vs.length - 1])} · ${points.length} points` }),
      h("span", { text: fmtTime(t1, "full") })));
}

export function mobiusPanel(gid) {
  if (!store.meta.mobius_explore) return null;
  const search = h("input", { type: "search", class: "mob-search", placeholder: "Filter: field, value or table",
    "aria-label": "Filter the Mobius values" });
  let fresh = false;
  const seg = h("div", { class: "seg light", role: "group", "aria-label": "Which values" },
    ...[[false, "Everything kept"], [true, "Fresh (10 min)"]].map(([v, t]) => h("button", { type: "button", text: t, "aria-pressed": String(v === fresh),
      onclick: () => { fresh = v; for (const b of seg.children) b.setAttribute("aria-pressed", String(b.textContent === t)); draw(); } })));
  const csv = h("button", { type: "button", class: "btn", text: "Download CSV", onclick: () => saveCsv() });
  const hint = h("span", { class: "hint" });
  const count = h("p", { class: "muted small mob-count" });
  const body = h("div", { class: "panel table-scroll mob-body" });
  const el = h("section", { class: "section" }, h("h2", {}, "Mobius data", hint),
    h("div", { class: "mob-tools" }, search, seg, csv), count, body);
  let data = null;
  let timer = null;
  let open = null;                 // the row whose chart is open: { key, row, span, el }
  const visible = () => {
    const now = Date.now() / 1000;
    return matches((data?.rows || []).filter((r) => !fresh || now - r.t <= FRESH_S), search.value);
  };
  function draw() {
    const now = Date.now() / 1000;
    clear(body);
    if (!data) {
      el.classList.toggle("hidden", store.groups[gid]?.tab === "infrastructure");
      body.append(h("p", { class: "muted", text: "Asking Mobius…" }));
      return;
    }
    if (data.error) body.append(h("p", { class: "ink2", text: `Mobius InfluxDB: ${data.error}` }));
    const all = data.rows || [];
    // a trailer or tower usually has nothing in Mobius: no empty section there
    el.classList.toggle("hidden", !all.length && !data.error && store.groups[gid]?.tab === "infrastructure");
    setText(hint, all.length ? `newest ${fmtAgo(now - data.t)} · shown as Mobius sends them` : "");
    const rows = all.filter((r) => !fresh || now - r.t <= FRESH_S);
    const tables = new Set(rows.map((r) => r.m)).size;
    setText(count, all.length ? `${rows.length.toLocaleString()} value${rows.length === 1 ? "" : "s"} in ${tables} table${tables === 1 ? "" : "s"}`
      + (fresh ? " from the last 10 minutes" : ` (Mobius has ${data.tables_read ?? "?"} tables; each field's last value is kept for a week)`)
      + " · click a number for its chart" : "");
    csv.disabled = !rows.length;
    if (!all.length) {
      body.append(h("p", { class: "muted", text: "Mobius has nothing for this in the last week. It is matched by its Mobius asset "
        + "or name - the Mobius Server's page lists the names Mobius uses that are not in the inventory." }));
      return;
    }
    const t = valuesTable(rows, search.value, now, { onValue: toggleChart, open: open?.key, chart: open?.el });
    body.append(t.shown ? t.table : h("p", { class: "muted", text: fresh ? "Nothing from the last 10 minutes matches - try Everything kept."
      : "No value matches the filter." }));
  }
  function toggleChart(row) {
    if (open && open.key === keyOf(row)) {
      open = null;
      draw();
      return;
    }
    open = { key: keyOf(row), row, span: "6h", el: h("div", { class: "mob-chart" }) };
    loadChart();
    draw();
  }
  async function loadChart() {
    const o = open;
    if (!o) return;
    const spans = h("div", { class: "seg light", role: "group", "aria-label": "Span" },
      ...SPANS.map((sp) => h("button", { type: "button", text: sp, "aria-pressed": String(sp === o.span),
        onclick: () => { o.span = sp; loadChart(); } })));
    o.el.replaceChildren(h("div", { class: "mob-chart-head" }, h("b", { text: `${o.row.m} · ${o.row.f}` }), spans),
      h("p", { class: "muted small", text: "Asking Mobius…" }));
    let res;
    try {
      res = await get(`/api/mobius/${encodeURIComponent(gid)}/history`, { m: o.row.m, f: o.row.f, span: o.span });
    } catch (e) {
      if (open === o) o.el.lastChild.replaceWith(h("p", { class: "ink2 small", text: e.message }));
      return;
    }
    if (open === o) o.el.lastChild.replaceWith(spark(res.points || []));
  }
  function saveCsv() {
    const rows = visible();
    const name = (store.groups[gid]?.name || gid).replace(/[^A-Za-z0-9._-]+/g, "_");
    downloadCsv(`mobius-${name}.csv`, ["table", "field", "value", "time (UTC)"],
      rows.map((r) => [r.m, r.f, isNum(r.v) ? r.v : fmtV(r.v), new Date(r.t * 1000).toISOString()]));
  }
  async function load() {
    try { data = await get(`/api/mobius/${gid}`); } catch (e) { data = { error: String(e.message || e), rows: [] }; }
    draw();
  }
  search.addEventListener("input", draw);
  draw();
  return { el, start() { load(); timer = setInterval(load, 30000); }, destroy() { clearInterval(timer); } };
}

export function mobiusEverything(nav) {
  if (!store.meta.mobius_explore) return null;
  const hint = h("span", { class: "hint" });
  const body = h("div", {});
  const el = h("section", { class: "section" }, h("h2", {}, "Everything Mobius reports", hint), body);
  let timer = null;
  async function load() {
    let d;
    try { d = await get("/api/mobius"); } catch (e) { d = { error: String(e.message || e) }; }
    const now = Date.now() / 1000;
    clear(body);
    setText(hint, d.last ? `${d.measurements} tables · read ${fmtAgo(now - d.last)}` : "");
    if (d.error) body.append(h("p", { class: "ink2", text: `Mobius InfluxDB: ${d.error}` }));
    const matched = Object.entries(d.groups || {}).map(([gid, x]) => ({ g: store.groups[gid], ...x })).filter((x) => x.g)
      .sort((a, b) => a.g.name.localeCompare(b.g.name));
    body.append(h("h3", { class: "mob-h3", text: `In the inventory (${matched.length})` }),
      matched.length ? h("div", { class: "panel table-scroll" }, h("table", { class: "comps" },
        h("thead", {}, h("tr", {}, ...["Vehicle / location / desk", "Values", "Newest"].map((t) => h("th", { text: t })))),
        h("tbody", {}, matched.map((x) => h("tr", {}, h("td", {}, link(x.g.name, `#/${x.g.tab}/${x.g.id}`, nav)),
          h("td", { class: "num", text: String(x.fields) }), h("td", { class: "muted", text: fmtAgo(now - x.t) }))))))
        : h("p", { class: "muted", text: "Nothing Mobius sends matches a name in the inventory yet." }));
    const extra = d.unmatched || [];
    body.append(h("h3", { class: "mob-h3", text: `Mobius knows, not in the inventory (${extra.length})` }),
      extra.length ? h("div", { class: "panel table-scroll" }, h("table", { class: "comps" },
        h("thead", {}, h("tr", {}, ...["Name in Mobius", "Tables", "Values", "Newest"].map((t) => h("th", { text: t })))),
        h("tbody", {}, extra.map((u) => h("tr", {}, h("td", {}, h("b", { text: u.value }), h("span", { class: "muted", text: ` (${u.tag})` })),
          h("td", { text: u.measurements.length > 6 ? `${u.measurements.slice(0, 6).join(", ")} … (${u.measurements.length})` : u.measurements.join(", ") }),
          h("td", { class: "num", text: String(u.fields) }),
          h("td", { class: "muted", text: u.t ? fmtWhen(u.t) : "–" }))))))
        : h("p", { class: "muted", text: "Every name Mobius uses is in the inventory." }));
    if (extra.length) {
      body.append(h("p", { class: "muted small", text: "To add one: devices.xlsx, Groups sheet - a vehicle's Mobius asset column, "
        + "or a client desk (Kind client) - then rebuild the inventory." }));
    }
    const site = d.site || [];
    if (site.length) {
      const t = valuesTable(site, "", now);
      body.append(h("details", { class: "mob-site" }, h("summary", { text: `Site-wide values (${site.length})` }),
        h("div", { class: "panel table-scroll mob-body" }, t.table)));
    }
  }
  return { el, start() { load(); timer = setInterval(load, 60000); }, destroy() { clearInterval(timer); } };
}
