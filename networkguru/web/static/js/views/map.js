// Map page: a pin per located site location (trailers, towers, buildings, ...) coloured by its live
// status, the vehicles where their GNSS last put them, and the 60 GHz links as lines. Hover for the
// reason, click for the location's page. Positions come from the Groups sheet (Lat / Lon, filled
// from the Rajant snapshot); vehicles from their GNSS component, else from lat / lon fields Mobius
// has for them (0.8.0). What has gone quiet is left out, as in Live (quiet.js).

import { get } from "../api.js";
import { h, s, setClass, setText } from "../dom.js";
import { STATUS_LABEL, statusName } from "../fmt.js";
import { isQuiet, setShowHidden, showHidden } from "../quiet.js";
import { dot } from "../status.js";
import { node, store, window_ } from "../store.js";
import { hideTip, showTip } from "../widgets/chart.js";
import { slippyMap } from "../widgets/slippy.js";

const GLYPH = {
  building: "M3 14V6.5L8 3l5 3.5V14H9.6v-3.6H6.4V14z",
  tower: "M7 2h2l3 12h-2.2L8 6.6 6.2 14H4z",
  repeater: "M7.1 7h1.8v7H7.1zM8 2.4a2.3 2.3 0 110 4.6 2.3 2.3 0 010-4.6z",
  trailer: "M1.5 4.5h10v6.5h-10zM11.5 7h2.2l1 1.4V11h-3.2zM4 13.4a1.4 1.4 0 110-2.8 1.4 1.4 0 010 2.8z",
  cow: "M1 5.5h8.6V11H1zM9.6 7.4h2.9L14.6 9.6V11h-5zM4 13.3a1.3 1.3 0 110-2.6 1.3 1.3 0 010 2.6zM11.6 13.3a1.3 1.3 0 110-2.6 1.3 1.3 0 010 2.6z",
  core: "M3 2.6h10v4.6H3zM3 8.8h10v4.6H3z",
  vehicle: "M8 1.8l4.6 12.2L8 11.2 3.4 14z",
  other: "M8 3a5 5 0 110 10A5 5 0 018 3z",
};
const KIND_TEXT = { trailer: "Trailer", tower: "Tower", building: "Building", repeater: "Repeater", cow: "COW",
  core: "Network core", server: "Server", client: "Desk", vehicle: "Vehicle" };

const glyphOf = (kind) => GLYPH[kind] || (kind === "server" || kind === "client" ? GLYPH.core : GLYPH.other);
const located = (g) => typeof g.lat === "number" && typeof g.lon === "number";

function vehiclePos(g) {
  for (const c of g.components) {
    const i = node(c.id).i || {};
    if (typeof i.lat === "number" && typeof i.lon === "number") return { lat: i.lat, lon: i.lon };
  }
  const m = store.mobius?.positions?.[g.id];
  return m ? { lat: m.lat, lon: m.lon } : null;
}

const away = (g) => isQuiet(g, { always: true }) && !showHidden("map");

export function mapView(root, nav) {
  const head = h("div", { class: "page-head" }, h("h1", { text: "Map" }), h("span", { class: "sub", text: store.inv.site.name }));
  const note = h("div", { class: "map-note hidden", role: "status" });
  const side = h("aside", { class: "map-side panel pad" });
  const holder = h("div", { class: "map-holder" });
  root.append(head, note, h("div", { class: "map-layout" }, holder, side));

  const groups = Object.values(store.groups);
  const vehicles = groups.filter((g) => g.tab === "vehicles");
  const places = groups.filter((g) => g.tab !== "vehicles" && located(g));
  let map = null;
  let extra = "";                  // why the background is plain, when it is
  const pinOf = {};                // group id -> { btn, label }
  const lines = [];                // { a, b, comps, el }

  function notes() {
    const parts = [];
    if (!window_().live) parts.push("The map shows the live state, also in History.");
    if (extra) parts.push(extra);
    if (!places.length) parts.push("No location has a position yet: add Lat / Lon on the Groups sheet of devices.xlsx "
      + "(the Rajant snapshot import fills them from the radios' GPS), then rebuild the inventory.");
    setText(note, parts.join(" "));
    note.classList.toggle("hidden", !parts.length);
  }

  function tip(e, g) {
    const n = node(g.id);
    showTip(e.clientX, e.clientY, g.name, [
      { value: STATUS_LABEL[statusName(n.s)], label: (n.r || [])[0] || "" },
      { value: KIND_TEXT[g.kind] || (g.tab === "vehicles" ? "Vehicle" : "Location"), label: "" }]);
  }

  function makePin(g, small) {
    const kind = g.tab === "vehicles" ? "vehicle" : g.kind;
    const btn = h("button", { type: "button", class: `pin${small ? " small" : ""}`, "aria-label": g.name,
      onclick: () => nav(`#/${g.tab}/${g.id}`),
      onmouseenter: (e) => tip(e, g), onmousemove: (e) => tip(e, g), onmouseleave: hideTip,
      onfocus: (e) => { const r = e.target.getBoundingClientRect(); tip({ clientX: r.right, clientY: r.top }, g); },
      onblur: hideTip },
      s("svg", { viewBox: "0 0 16 16", "aria-hidden": "true" }, s("path", { d: glyphOf(kind) })),
      h("span", { class: "pin-label", text: g.name }));
    map.pins.append(btn);
    pinOf[g.id] = { btn };
    return btn;
  }

  function buildLines() {
    const seen = new Set();
    for (const g of places) {
      for (const c of g.components) {
        const p = c.partner && store.comps[c.partner];
        if (!p) continue;
        const other = store.groups[p.group];
        if (!other || other.id === g.id || !located(other)) continue;
        const key = [c.id, p.id].sort().join("|");
        if (seen.has(key)) continue;
        seen.add(key);
        const el = s("line", { class: "map-link" });
        map.overlay.append(el);
        lines.push({ a: g, b: other, comps: [c.id, p.id], el });
      }
    }
  }

  function draw(project) {
    for (const g of places) {
      const { x, y } = project(g.lat, g.lon);
      pinOf[g.id].btn.style.transform = `translate(${x}px, ${y}px)`;
      pinOf[g.id].btn.classList.toggle("hidden", away(g));
    }
    for (const g of vehicles) {
      const pos = vehiclePos(g);
      const pin = pinOf[g.id];
      if (!pin) continue;
      pin.btn.classList.toggle("hidden", !pos || away(g));
      if (pos) {
        const { x, y } = project(pos.lat, pos.lon);
        pin.btn.style.transform = `translate(${x}px, ${y}px)`;
      }
    }
    for (const l of lines) {
      const a = project(l.a.lat, l.a.lon), b = project(l.b.lat, l.b.lon);
      l.el.setAttribute("x1", a.x); l.el.setAttribute("y1", a.y);
      l.el.setAttribute("x2", b.x); l.el.setAttribute("y2", b.y);
    }
  }

  function colour() {
    for (const [gid, pin] of Object.entries(pinOf)) setClass(pin.btn, "s-", statusName(node(gid).s));
    for (const l of lines) {
      const worst = Math.max(...l.comps.map((c) => node(c).s));
      setClass(l.el, "s-", statusName(worst));
      l.el.classList.toggle("down", worst === 2 || worst === 4);
    }
  }

  function fillSide() {
    const quiet = groups.filter((g) => isQuiet(g, { always: true }));
    const hiding = !showHidden("map");
    const missing = groups.filter((g) => (g.tab === "vehicles" ? !vehiclePos(g) : !located(g)) && !(hiding && quiet.includes(g)));
    side.replaceChildren(
      h("h2", { text: "Legend" }),
      h("ul", { class: "plain map-legend" }, ...["ok", "warning", "critical", "offline", "unknown"].map((st) =>
        h("li", {}, dot(["ok", "unknown", "offline", "warning", "critical"].indexOf(st)), STATUS_LABEL[st]))),
      h("p", { class: "muted small", text: "Lines are 60 GHz links. Vehicles show where their GNSS (or Mobius) last put them." }),
      h("h2", { text: `Not on the map (${missing.length})` }),
      missing.length ? h("ul", { class: "plain map-missing" }, ...missing.map((g) =>
        h("li", {}, dot(node(g.id).s), h("a", { href: `#/${g.tab}/${g.id}`, text: g.name,
          onclick: (e) => { e.preventDefault(); nav(`#/${g.tab}/${g.id}`); } }))))
        : h("p", { class: "muted small", text: "Everything has a position." }),
      h("p", { class: "muted small", text: "Positions: the Lat / Lon columns on the Groups sheet of devices.xlsx." }),
      quiet.length ? h("div", { class: "map-quiet" },
        h("h2", { text: `Gone quiet (${quiet.length})` }),
        h("p", { class: "muted small", text: hiding ? "No answer for a while - left off the map, as in Live." : "Shown on the map." }),
        h("button", { type: "button", class: "btn", text: hiding ? "Show them" : "Hide them",
          onclick: () => { setShowHidden("map", hiding); fillSide(); map?.render(); } })) : null);
  }

  let alive = true;
  (async () => {
    let cfg = { tiles: null, background: "none" };
    try { cfg = await get("/api/map"); } catch { /* plain map */ }
    if (!alive) return;
    const site = store.inv.site;
    map = slippyMap({ tiles: cfg.tiles, center: { lat: site.lat, lon: site.lon }, zoom: 15,
      onTilesFailed: () => { extra = "Map images unavailable (no internet?) - positions are by GPS."; notes(); } });
    holder.append(map.el, h("div", { class: "map-buttons" },
      h("button", { type: "button", class: "btn", text: "Fit the site", onclick: () => fitAll() })));
    for (const g of places) makePin(g, false);
    for (const g of vehicles) makePin(g, true);
    buildLines();
    colour();
    fillSide();
    extra = cfg.tiles ? "" : "No map background is chosen (Admin → Map) - positions are by GPS.";
    notes();
    map.onRender(draw);
    fitAll();
  })();

  function fitAll() {
    if (!map) return;
    const pts = places.map((g) => ({ lat: g.lat, lon: g.lon }));
    for (const g of vehicles) { const p = vehiclePos(g); if (p) pts.push(p); }
    if (pts.length) map.fit(pts);
  }

  let lastSide = 0;
  return {
    update() {
      if (!map) return;
      colour();
      map.render();
      const now = Date.now();
      if (now - lastSide > 5000) { lastSide = now; fillSide(); }
    },
    refreshRange() { notes(); },
    destroy() { alive = false; hideTip(); map?.destroy(); },
  };
}
