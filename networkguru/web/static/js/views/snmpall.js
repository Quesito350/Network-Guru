// A device page's "All SNMP values" (0.11.0, drivers/snmp_walk.py): Read all values asks the server to read
// everything the device offers (system, interfaces, PoE, sensors, hardware, the vendor's own values) once;
// the table filters by name, number or value and by section, and downloads as CSV. Rajants, Tachyons and
// switches in live mode; a cell modem never (its SIM numbers stay unread).

import { get, post } from "../api.js";
import { downloadCsv } from "../csv.js";
import { clear, h, setText } from "../dom.js";
import { fmtAgo } from "../fmt.js";
import { store } from "../store.js";

const TYPES = ["rajant", "tachyon", "switch"];
const SECTIONS = ["System", "Interfaces", "PoE", "Sensors", "Hardware", "Vendor"];

export function snmpAllPanel(c) {
  if (!TYPES.includes(c.type) || store.meta.mode !== "live" || !c.host) return null;
  const read = h("button", { type: "button", class: "btn primary", text: "Read all values", onclick: () => start() });
  const status = h("p", { class: "muted small", role: "status" });
  const search = h("input", { type: "search", class: "mob-search", placeholder: "Filter: name, number or value",
    "aria-label": "Filter the SNMP values" });
  const seg = h("div", { class: "seg light", role: "group", "aria-label": "Section" });
  const csv = h("button", { type: "button", class: "btn", text: "Download CSV", onclick: () => saveCsv() });
  const tools = h("div", { class: "mob-tools hidden" }, search, seg, csv);
  const count = h("p", { class: "muted small mob-count" });
  const body = h("div", { class: "panel table-scroll mob-body hidden" });
  const el = h("section", { class: "section" }, h("h2", { text: "All SNMP values" }),
    h("p", { class: "muted small", text: "Everything this device offers over SNMP - read once when you ask (it can take up to "
      + "a minute over the radio links), not on every check. Values are shown as the device sends them." }),
    h("div", { class: "actions-row" }, read), status, tools, count, body);
  let data = null;
  let section = "All";
  let timer = null;

  const shownRows = () => {
    const f = search.value.trim().toLowerCase();
    return (data?.rows || []).filter((r) => (section === "All" || r.s === section)
      && (!f || r.name.toLowerCase().includes(f) || r.oid.includes(f) || String(r.v).toLowerCase().includes(f)));
  };

  function drawSeg() {
    const have = new Set((data?.rows || []).map((r) => r.s));
    seg.replaceChildren(...["All", ...SECTIONS.filter((x) => have.has(x))].map((x) => h("button", { type: "button", text: x,
      "aria-pressed": String(x === section), onclick: () => { section = x; drawSeg(); drawTable(); } })));
  }

  function drawTable() {
    const rows = shownRows();
    const all = data?.rows || [];
    setText(count, `${rows.length.toLocaleString()} of ${all.length.toLocaleString()} values`);
    clear(body);
    if (!rows.length) {
      body.append(h("p", { class: "muted", text: all.length ? "No value matches the filter." : "The device sent no values." }));
      return;
    }
    const limit = 2000;                     // a filter narrows it; the CSV always has all that match
    body.append(h("table", { class: "info-table snmp-table" },
      h("thead", {}, h("tr", {}, ...["Name", "Value", "OID"].map((t) => h("th", { text: t })))),
      h("tbody", {}, rows.slice(0, limit).map((r) => h("tr", { class: r.k === "hidden" ? "stale" : "" },
        h("td", { text: r.name || "–" }),
        h("td", { class: r.k === "number" ? "num" : "" }, h("b", { text: String(r.v) })),
        h("td", { class: "muted mono", text: r.oid }))))));
    if (rows.length > limit) body.append(h("p", { class: "muted small", text: `First ${limit.toLocaleString()} shown - filter to narrow it, or download the CSV.` }));
  }

  function draw() {
    const now = Date.now() / 1000;
    const st = data?.state || "none";
    read.disabled = st === "running";
    setText(read, st === "running" ? "Reading…" : st === "done" ? "Read again" : "Read all values");
    if (st === "running") setText(status, `Reading every value with ${data.label || "SNMP"}… (started ${fmtAgo(now - data.t)})`);
    else if (st === "error") setText(status, `Could not read it: ${data.error}`);
    else if (st === "done") {
      setText(status, `Read ${fmtAgo(now - data.t)} in ${data.took_s} s with ${data.label}`
        + (data.cut ? " - cut short at the limit (5,000 values or 90 s)" : "")
        + (data.skipped?.length ? `. Not answered: ${data.skipped.join("; ")}` : "")
        + (now - data.t < data.keep_s ? ` · Read again after ${Math.ceil((data.keep_s - (now - data.t)) / 60)} min shows a new read` : ""));
    } else setText(status, "");
    const show = st === "done";
    tools.classList.toggle("hidden", !show);
    body.classList.toggle("hidden", !show);
    count.classList.toggle("hidden", !show);
    if (show) { drawSeg(); drawTable(); }
  }

  async function load() {
    try { data = await get(`/api/snmp/${encodeURIComponent(c.id)}/all`); } catch (e) { data = { state: "error", error: e.message }; }
    draw();
    clearTimeout(timer);
    if (data.state === "running") timer = setTimeout(load, 1500);
  }

  async function start() {
    read.disabled = true;
    try { data = await post(`/api/snmp/${encodeURIComponent(c.id)}/all`); } catch (e) {
      setText(status, e.message);
      read.disabled = false;
      return;
    }
    draw();
    if (data.state === "running") timer = setTimeout(load, 1500);
  }

  function saveCsv() {
    const name = (c.name || c.id).replace(/[^A-Za-z0-9._-]+/g, "_");
    downloadCsv(`snmp-${name}.csv`, ["section", "name", "value", "oid"], shownRows().map((r) => [r.s, r.name, r.v, r.oid]));
  }

  search.addEventListener("input", drawTable);
  return { el, start() { load(); }, destroy() { clearTimeout(timer); } };
}
