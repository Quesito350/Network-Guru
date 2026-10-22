// Mobius data on the group pages (0.8.0): every value the Mobius InfluxDB has for a vehicle, location
// or desk - the newest of each field in the last 10 minutes, from the server's Mobius explorer. Shown
// as it is: no history and no status come from these (that is the Mobius mapping's job).
// The Mobius Server's page also lists everything Mobius reports, including names not in the inventory.

import { get } from "../api.js";
import { clear, h, setText } from "../dom.js";
import { fmtAgo, fmtWhen } from "../fmt.js";
import { store } from "../store.js";

const STALE_S = 120;

function fmtV(v) {
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : String(Number(v.toPrecision(9)));   // keeps GPS digits
  return String(v ?? "");
}

function link(text, href, nav) {
  const a = h("a", { href, text });
  a.addEventListener("click", (e) => { e.preventDefault(); nav(href); });
  return a;
}

function valuesTable(rows, filter, now) {
  const f = filter.trim().toLowerCase();
  const shown = rows.filter((r) => !f || r.f.toLowerCase().includes(f) || r.m.toLowerCase().includes(f)
    || fmtV(r.v).toLowerCase().includes(f));
  const body = [];
  let last = null;
  for (const r of shown) {
    if (r.m !== last) {
      body.push(h("tr", { class: "mob-meas" }, h("th", { colspan: "3", text: r.m })));
      last = r.m;
    }
    body.push(h("tr", { class: now - r.t > STALE_S ? "stale" : "" }, h("td", { text: r.f }),
      h("td", { class: "num" }, h("b", { text: fmtV(r.v) })), h("td", { class: "muted", text: fmtAgo(now - r.t) })));
  }
  return { table: h("table", { class: "info-table mob-table" }, h("tbody", {}, body)), shown: shown.length };
}

export function mobiusPanel(gid) {
  if (!store.meta.mobius_explore) return null;
  const search = h("input", { type: "search", class: "mob-search", placeholder: "Filter: field, value or measurement",
    "aria-label": "Filter the Mobius values" });
  const hint = h("span", { class: "hint" });
  const body = h("div", { class: "panel table-scroll mob-body" });
  const el = h("section", { class: "section" }, h("h2", {}, "Mobius data", hint), search, body);
  let data = null;
  let timer = null;
  function draw() {
    const now = Date.now() / 1000;
    clear(body);
    if (!data) {
      el.classList.toggle("hidden", store.groups[gid]?.tab === "infrastructure");
      body.append(h("p", { class: "muted", text: "Asking Mobius…" }));
      return;
    }
    if (data.error) body.append(h("p", { class: "ink2", text: `Mobius InfluxDB: ${data.error}` }));
    const rows = data.rows || [];
    // a trailer or tower usually has nothing in Mobius: no empty section there
    el.classList.toggle("hidden", !rows.length && !data.error && store.groups[gid]?.tab === "infrastructure");
    setText(hint, rows.length ? `${rows.length} values from the Mobius InfluxDB · newest ${fmtAgo(now - data.t)} · shown as Mobius sends them` : "");
    if (!rows.length) {
      body.append(h("p", { class: "muted", text: "Mobius has nothing for this in the last 10 minutes. It is matched by its Mobius asset "
        + "or name - the Mobius Server's page lists the names Mobius uses that are not in the inventory." }));
      return;
    }
    const t = valuesTable(rows, search.value, now);
    body.append(t.shown ? t.table : h("p", { class: "muted", text: "No value matches the filter." }));
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
    setText(hint, d.last ? `${d.measurements} measurements · read ${fmtAgo(now - d.last)}` : "");
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
        h("thead", {}, h("tr", {}, ...["Name in Mobius", "Measurements", "Values", "Newest"].map((t) => h("th", { text: t })))),
        h("tbody", {}, extra.map((u) => h("tr", {}, h("td", {}, h("b", { text: u.value }), h("span", { class: "muted", text: ` (${u.tag})` })),
          h("td", { text: u.measurements.join(", ") }), h("td", { class: "num", text: String(u.fields) }),
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
