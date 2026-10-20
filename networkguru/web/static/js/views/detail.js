// Detail pages: a vehicle, an infrastructure location, a control-room station, one component.

import { get, post } from "../api.js";
import { h, setText } from "../dom.js";
import { fmtValue, fmtWhen, metricLevel, typeSpec } from "../fmt.js";
import { badge } from "../status.js";
import { node, store, window_ } from "../store.js";
import { pathWidget } from "../widgets/path.js";
import { battery } from "../widgets/small.js";
import { chartsSection, compTable, detailHeader, eventsSection, TAB_TITLE, vehicleHops } from "./common.js";

function groupFacts(g) {
  return () => {
    const f = [];
    if (g.tab === "vehicles") {
      f.push(["Model", g.model], ["Network", (g.access || "rajant") === "celona" ? "Celona private LTE" : "Rajant mesh"],
        ["Mobius asset", g.mobius_asset]);
      if (g.expected_online) f.push(["Expected online", g.expected_online]);
    } else {
      f.push(["Components", String(g.components.length)]);
      if (g.expected_online) f.push(["Expected online", g.expected_online]);
    }
    const seen = g.components.map((c) => node(c.id).seen).filter(Boolean);
    if (seen.length) f.push(["Last data", fmtWhen(Math.max(...seen))]);
    return f;
  };
}

function simTools(targetId, label, hasLink = false) {
  if (store.meta.mode !== "sim") return null;
  const kind = h("select", { class: "btn", "aria-label": "Fault type" },
    h("option", { value: "outage", text: "Outage" }), h("option", { value: "degrade", text: "Degraded link" }),
    hasLink ? h("option", { value: "link", text: "60 GHz link down (radios stay up)" }) : null);
  const mins = h("select", { class: "btn", "aria-label": "Duration" },
    ...[2, 5, 15, 30].map((m) => h("option", { value: m, text: `${m} min` })));
  const msg = h("span");
  const btn = h("button", { class: "btn", type: "button", text: "Inject" });
  btn.addEventListener("click", async () => {
    try {
      await post("/api/sim/fault", { target: targetId, kind: kind.value, minutes: Number(mins.value) });
      setText(msg, `Injected for ${mins.value} min - ${label} changes within about 30 s (3 missed polls).`);
    } catch (e) { setText(msg, String(e)); }
  });
  return h("div", { class: "panel sim-tools" }, h("b", { text: "Simulator:" }), "test a fault on", h("b", { text: label }), kind, mins, btn, msg);
}

function mount(root, parts) {
  for (const p of parts) if (p) root.append(p.el || p);
}

// ---------------------------------------------------------------------------
export function groupView(root, gid, nav) {
  const g = store.groups[gid];
  if (!g) {
    root.append(h("p", { text: "Not found." }));
    return {};
  }
  const head = detailHeader({ id: gid, title: g.name, crumbHref: `#/${g.tab}`, crumbText: TAB_TITLE[g.tab], facts: groupFacts(g) });
  const table = compTable(g.components, nav);
  const parts = [head];
  let path = null;
  let pathHops = null;
  if (g.tab === "vehicles") {
    pathHops = vehicleHops(gid);
    path = pathWidget(pathHops, nav);
    parts.push(h("section", { class: "section" }, h("h2", { text: "Path from the Mobius Server" }), path.el));
  }
  const gx = g.components.find((c) => c.type === "victron_cerbo");
  let power = null;
  if (gx) power = powerPanel(gx);
  if (power) parts.push(power);
  const links = linksPanel(g, nav);
  if (links) parts.push(links);
  parts.push(h("section", { class: "section" }, h("h2", { text: "Components" }), table.el));
  const chartItems = g.components.filter((c) => c.type !== "victron_cerbo").map((c) => ({ comp: c.id, label: c.name }));
  const charts = chartsSection(chartItems, { primary: true, title: "Key charts" });
  parts.push(charts.el);
  const ev = eventsSection(gid, nav);
  parts.push(ev.el);
  parts.push(simTools(gid, g.name));
  mount(root, parts);

  function update() {
    head.update();
    table.update();
    if (path && window_().live) path.live();
    power?.update?.();
    links?.update();
  }
  async function loadHist() {
    head.history();
    ev.load();
    if (path && !window_().live) {
      const w = window_();
      path.history(await get(`/api/path/${gid}`, { from: w.from, to: w.to }));
    }
  }
  update();
  charts.start();
  power?.start?.();
  loadHist();
  return {
    update,
    destroy() { charts.destroy(); power?.destroy?.(); },
  };
}

// 60 GHz point-to-point links: both ends side by side, against the signal measured at install.
function linksPanel(g, nav) {
  const ends = g.components.filter((c) => c.link);
  if (!ends.length) return null;
  const rows = ends.map((c) => {
    const partner = c.partner ? store.comps[c.partner] : null;
    const cells = { st: h("td"), here: h("td", { class: "num" }), far: h("td", { class: "num" }), mcs: h("td", { class: "num" }) };
    let farCell = h("span", { class: "muted", text: "not in the inventory" });
    if (partner) {
      const href = `#/${partner.tab}/${partner.group}`;
      farCell = h("a", { href, text: `${store.groups[partner.group].name} · ${partner.name}` });
      farCell.addEventListener("click", (e) => { e.preventDefault(); nav(href); });
    }
    const tr = h("tr", {}, cells.st,
      h("td", {}, h("b", { text: c.link }), h("div", { class: "muted", style: "font-size:12px", text: `${c.name}, ${c.role || "?"} end` })),
      cells.here, h("td", {}, farCell), cells.far, cells.mcs,
      h("td", { class: "num ink2", text: c.install_rssi != null ? `${c.install_rssi} dBm` : "–" }));
    return { c, partner, cells, tr };
  });
  const redundant = store.inv.site.backhaul === "redundant";
  const sig = (id) => {
    const n = node(id);
    if (n.m?.tachyon_link_up === 0) {
      return h("span", { class: "ink2", text: redundant ? "link down - traffic on the Rajant mesh" : "link down" });
    }
    const v = n.m?.tachyon_rssi_dbm;
    if (v == null) return h("span", { class: "muted", text: "–" });
    const lvl = metricLevel("tachyon_rssi_dbm", v);
    return h("span", {}, fmtValue("tachyon_rssi_dbm", v), lvl ? h("span", { class: `mdot s-${lvl}`, style: "margin-left:5px" }) : null);
  };
  const el = h("section", { class: "section" },
    h("h2", {}, "60 GHz links", h("span", { class: "hint", text: "Tachyon pairs · warning 6 dB below the install signal, critical 12 dB"
      + (redundant ? " · management also runs over the Rajant mesh, so a link down is a Warning" : "") })),
    h("div", { class: "panel table-scroll" }, h("table", { class: "comps links" },
      h("thead", {}, h("tr", {}, ...["Status", "Link", "Signal here", "Far end", "Signal there", "MCS here", "At install"].map((t) => h("th", { text: t })))),
      h("tbody", {}, rows.map((r) => r.tr)))));
  return {
    el,
    update() {
      for (const r of rows) {
        const n = node(r.c.id);
        const worst = r.partner ? Math.max(n.s, node(r.partner.id).s) : n.s;
        r.cells.st.replaceChildren(badge(worst, { small: true }));
        r.cells.here.replaceChildren(sig(r.c.id));
        r.cells.far.replaceChildren(r.partner ? sig(r.partner.id) : "–");
        setText(r.cells.mcs, n.m?.tachyon_tx_mcs == null ? "–" : String(Math.round(n.m.tachyon_tx_mcs)));
      }
    },
  };
}

// Solar + battery from the Cerbo GX: big figures, then SOC, solar vs load, charger, daily yield.
function powerPanel(gx) {
  const battWrap = h("div", {});
  const figs = {};
  const top = h("div", { class: "power-top" }, battWrap);
  for (const [k, label] of [["power_pv_w", "Solar now"], ["power_load_w", "Load now"], ["power_batt_v", "Battery voltage"],
    ["power_batt_a", "Battery current"], ["power_mppt_state", "Charger"], ["power_yield_today_kwh", "Solar today"]]) {
    const v = h("div", { class: "v num" });
    figs[k] = v;
    top.append(h("div", { class: "fig" }, h("div", { class: "k", text: label }), v));
  }
  const charts = chartsSection([{ comp: gx.id, label: gx.name }], { primary: false, title: "Solar and battery charts" });
  const el = h("section", { class: "section" }, h("h2", {}, "Solar and battery", h("span", { class: "hint", text: `${gx.name} · Victron` })),
    h("div", { class: "panel", style: "margin-bottom:12px" }, top), charts.el);
  return {
    el,
    update() {
      const m = node(gx.id).m || {};
      battWrap.replaceChildren(h("div", {}, h("div", { class: "k", style: "font-size:12px;color:var(--ink-2)", text: "Battery" }),
        battery(m.power_soc_pct, { big: true })));
      for (const [k, v] of Object.entries(figs)) {
        setText(v, m[k] == null ? "–" : fmtValue(k, m[k]));
        v.style.color = metricLevel(k, m[k]) === "critical" ? "var(--s-critical)" : "";
      }
    },
    start: charts.start,
    destroy: charts.destroy,
  };
}

// ---------------------------------------------------------------------------
export function componentView(root, cid, nav) {
  const c = store.comps[cid];
  if (!c) {
    root.append(h("p", { text: "Not found." }));
    return {};
  }
  const g = store.groups[c.group];
  const head = detailHeader({
    id: cid, title: `${g.name} · ${c.name}`, crumbHref: `#/${g.tab}/${g.id}`, crumbText: g.name,
    facts: () => {
      const n = node(cid);
      const addr = n.i?.address || c.host;
      const partner = c.partner ? store.comps[c.partner] : null;
      const f = [["Type", typeSpec(c.type).label], ["Model", c.model],
        ["Address", addr ? `${addr} (${c.address === "dhcp" ? "DHCP" : "static"})` : (c.source === "mobius" ? null : "none yet")],
        ["Serial", c.serial], ["MAC", c.mac],
        ["Link", c.link ? `${c.link}, ${c.role || "?"} end${partner ? ` - far end ${store.groups[partner.group].name} · ${partner.name}` : ""}` : null],
        ["Install signal", c.install_rssi != null ? `${c.install_rssi} dBm` : null], ["Switch port", c.port],
        ["Data from", c.source === "mobius" ? "Mobius InfluxDB" : "the device"], ["Notes", c.notes]];
      const skip = new Set(["address", "link", "role"]);
      for (const [k, v] of Object.entries(n.i || {})) if (!skip.has(k) && v !== "" && v != null) f.push([k.replace(/_/g, " "), v]);
      if (n.seen) f.push(["Last seen", fmtWhen(n.seen)]);
      if (n.err) f.push(["Last error", n.err]);
      return f;
    },
  });
  const now = h("div", { class: "panel table-scroll", style: "padding:6px 14px" });
  const charts = chartsSection([{ comp: cid, label: "" }], { primary: false, title: "Charts" });
  const ev = eventsSection(cid, nav);
  mount(root, [head, h("section", { class: "section" }, h("h2", { text: "Current readings" }), now), charts.el, ev.el,
    simTools(cid, c.name, Boolean(c.link))]);
  function update() {
    head.update();
    const n = node(cid);
    const rows = Object.entries(n.m || {}).map(([k, v]) => h("tr", {}, h("td", { text: (store.meta.catalog.metrics[k.split("{")[0]]?.label) || k }),
      h("td", { class: "num" }, h("b", { text: fmtValue(k, v) }), metricLevel(k, v) ? h("span", { class: `mdot s-${metricLevel(k, v)}`, style: "margin-left:6px" }) : null)));
    now.replaceChildren(rows.length ? h("table", { class: "info-table" }, h("tbody", {}, rows))
      : h("p", { class: "muted", text: n.s === 2 ? "Not responding." : "No readings yet." }));
  }
  update();
  charts.start();
  head.history();
  ev.load();
  return { update, destroy() { charts.destroy(); } };
}
