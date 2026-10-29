// Map page: a pin per located site location (trailers, towers, buildings, ...) coloured by its live
// status, every vehicle where its GPS last put it, and the 60 GHz links as lines. Hover for the
// reason, click for the location's or vehicle's page. Positions come from the Groups sheet (Lat / Lon,
// filled from the Rajant snapshot); vehicles from the Mobius InfluxDB (0.9.3: /api/map/vehicles - every
// Mobius vehicle, its last position kept across restarts), else their GNSS component (the simulator),
// else lat / lon fields Mobius has for them (0.8.0). A parked vehicle stays at its last position, faded
// ("Hide them" takes them off); a Mobius vehicle that is not in the inventory gets a grey pin with its
// Mobius name. Locations that have gone quiet are left out, as in Live (quiet.js).

import { get } from "../api.js";
import { h, s, setClass, setText } from "../dom.js";
import { fmtAgo, fmtWhen, STATUS_LABEL, statusName } from "../fmt.js";
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
const FOREIGN_MAX_AGE_S = 7 * 86400;     // a Mobius vehicle not in the inventory: on the map while this fresh
const FLEET_EVERY_MS = 10000;

const glyphOf = (kind) => GLYPH[kind] || (kind === "server" || kind === "client" ? GLYPH.core : GLYPH.other);
const located = (g) => typeof g.lat === "number" && typeof g.lon === "number";

function lsGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } }
const hideParked = () => lsGet("ng_map_hide_parked") === "1";

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
  const pinOf = {};                // group id -> { btn }
  const foreignPin = {};           // Mobius name -> button (in Mobius, not in the inventory)
  const lines = [];                // { a, b, comps, el }
  let fleet = { enabled: false, vehicles: [], byGroup: {}, byName: {} };

  // where a vehicle is: Mobius (live or its last position), else its GNSS info, else the explorer
  function vehiclePos(g) {
    const f = fleet.byGroup[g.id];
    if (f) return f;
    for (const c of g.components) {
      const i = node(c.id).i || {};
      if (typeof i.lat === "number" && typeof i.lon === "number") return { lat: i.lat, lon: i.lon, t: null, live: null };
    }
    const m = store.mobius?.positions?.[g.id];
    return m ? { lat: m.lat, lon: m.lon, t: m.t, live: null } : null;
  }
  // switched off, not talking to Mobius, or quiet: stays at its last position, faded
  const parked = (g) => node(g.id).s === 2 || vehiclePos(g)?.live === false || isQuiet(g, { always: true });

  function foreign() {
    const now = Date.now() / 1000;
    return fleet.vehicles.filter((v) => !v.group && now - v.t <= FOREIGN_MAX_AGE_S);
  }

  function notes() {
    const parts = [];
    if (!window_().live) parts.push("The map shows the live state, also in History.");
    if (extra) parts.push(extra);
    if (!places.length) parts.push("No location has a position yet: add Lat / Lon on the Groups sheet of devices.xlsx "
      + "(the Rajant snapshot import fills them from the radios' GPS), then rebuild the inventory.");
    setText(note, parts.join(" "));
    note.classList.toggle("hidden", !parts.length);
  }

  function posRow(p) {
    if (!p) return null;
    if (p.t == null) return { value: "Position", label: "from its GNSS" };
    return p.live === false || (p.live == null && Date.now() / 1000 - p.t > 600)
      ? { value: "Last position", label: fmtWhen(p.t) }
      : { value: "Position", label: fmtAgo(Date.now() / 1000 - p.t) };
  }

  function tip(e, g) {
    const n = node(g.id);
    const isVehicle = g.tab === "vehicles";
    showTip(e.clientX, e.clientY, g.name, [
      { value: STATUS_LABEL[statusName(n.s)], label: (n.r || [])[0] || "" },
      { value: KIND_TEXT[g.kind] || (isVehicle ? "Vehicle" : "Location"), label: isVehicle && parked(g) ? "parked" : "" },
      isVehicle ? posRow(vehiclePos(g)) : null].filter(Boolean));
  }

  function foreignTip(e, name) {
    const v = fleet.byName[name];
    showTip(e.clientX, e.clientY, name, [
      { value: "In Mobius", label: "not in the inventory" },
      { value: v?.live ? "Talking to Mobius" : "Not talking to Mobius", label: "" },
      posRow(v),
      { value: "", label: "Put this name in Groups → Mobius asset (devices.xlsx)" }].filter(Boolean));
  }

  const tipHandlers = (show) => ({
    onmouseenter: show, onmousemove: show, onmouseleave: hideTip,
    onfocus: (e) => { const r = e.target.getBoundingClientRect(); show({ clientX: r.right, clientY: r.top }); },
    onblur: hideTip,
  });

  function makePin(g, small) {
    const kind = g.tab === "vehicles" ? "vehicle" : g.kind;
    const btn = h("button", { type: "button", class: `pin${small ? " small" : ""}`, "aria-label": g.name,
      onclick: () => nav(`#/${g.tab}/${g.id}`), ...tipHandlers((e) => tip(e, g)) },
      s("svg", { viewBox: "0 0 16 16", "aria-hidden": "true" }, s("path", { d: glyphOf(kind) })),
      h("span", { class: "pin-label", text: g.name }));
    map.pins.append(btn);
    pinOf[g.id] = { btn };
    return btn;
  }

  function syncForeign() {
    const want = new Set(foreign().map((v) => v.name));
    for (const [name, btn] of Object.entries(foreignPin)) {
      if (!want.has(name)) { btn.remove(); delete foreignPin[name]; }
    }
    for (const name of want) {
      if (foreignPin[name]) continue;
      const btn = h("button", { type: "button", class: "pin small foreign", "aria-label": `${name} (in Mobius, not in the inventory)`,
        ...tipHandlers((e) => foreignTip(e, name)) },
        s("svg", { viewBox: "0 0 16 16", "aria-hidden": "true" }, s("path", { d: GLYPH.vehicle })),
        h("span", { class: "pin-label", text: name }));
      map.pins.append(btn);
      foreignPin[name] = btn;
    }
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
    const hide = hideParked();
    for (const g of vehicles) {
      const pos = vehiclePos(g);
      const pin = pinOf[g.id];
      if (!pin) continue;
      const off = !!pos && parked(g);
      pin.btn.classList.toggle("hidden", !pos || (off && hide));
      pin.btn.classList.toggle("parked", off);
      if (pos) {
        const { x, y } = project(pos.lat, pos.lon);
        pin.btn.style.transform = `translate(${x}px, ${y}px)`;
      }
    }
    for (const [name, btn] of Object.entries(foreignPin)) {
      const v = fleet.byName[name];
      if (!v) continue;
      btn.classList.toggle("parked", !v.live);
      btn.classList.toggle("hidden", !v.live && hide);
      const { x, y } = project(v.lat, v.lon);
      btn.style.transform = `translate(${x}px, ${y}px)`;
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

  const goTo = (g) => h("a", { href: `#/${g.tab}/${g.id}`, text: g.name,
    onclick: (e) => { e.preventDefault(); nav(`#/${g.tab}/${g.id}`); } });

  function fillSide() {
    const quiet = places.filter((g) => isQuiet(g, { always: true }));
    const hiding = !showHidden("map");
    const others = foreign();
    const parkedOnes = [...vehicles.filter((g) => vehiclePos(g) && parked(g)), ...others.filter((v) => !v.live)];
    const hideP = hideParked();
    const missing = groups.filter((g) => (g.tab === "vehicles" ? !vehiclePos(g) : !located(g))
      && !(g.tab !== "vehicles" && hiding && isQuiet(g, { always: true })));
    const now = Date.now() / 1000;
    side.replaceChildren(...[
      h("h2", { text: "Legend" }),
      h("ul", { class: "plain map-legend" }, ...["ok", "warning", "critical", "offline", "unknown"].map((st) =>
        h("li", {}, dot(["ok", "unknown", "offline", "warning", "critical"].indexOf(st)), STATUS_LABEL[st]))),
      h("p", { class: "muted small", text: "Lines are 60 GHz links. Vehicles are where their GPS (in Mobius) last put "
        + "them: faded = parked, at its last position. Grey = in Mobius, not in the inventory." }),
      parkedOnes.length ? h("div", { class: "map-parked" },
        h("h2", { text: `Parked vehicles (${parkedOnes.length})` }),
        h("p", { class: "muted small", text: hideP ? "Left off the map." : "At their last position, faded." }),
        h("button", { type: "button", class: "btn", text: hideP ? "Show them" : "Hide them",
          onclick: () => { lsSet("ng_map_hide_parked", hideP ? "0" : "1"); fillSide(); map?.render(); } })) : null,
      others.length ? h("div", { class: "map-foreign" },
        h("h2", { text: `In Mobius, not in the inventory (${others.length})` }),
        h("ul", { class: "plain" }, ...others.map((v) =>
          h("li", {}, dot(v.live ? 1 : 2), h("span", { text: v.name }),
            h("span", { class: "muted", text: v.live ? "now" : fmtWhen(v.t, now) })))),
        h("p", { class: "muted small", text: "Put a name in Groups → Mobius asset of devices.xlsx, then rebuild the "
          + "inventory: the vehicle then gets its own pin, cards and checks." })) : null,
      h("h2", { text: `Not on the map (${missing.length})` }),
      missing.length ? h("ul", { class: "plain map-missing" }, ...missing.map((g) => h("li", {}, dot(node(g.id).s), goTo(g))))
        : h("p", { class: "muted small", text: "Everything has a position." }),
      h("p", { class: "muted small", text: "Positions: the Lat / Lon columns on the Groups sheet of devices.xlsx; "
        + "vehicles: their GPS in Mobius (Groups → Mobius asset)." }),
      quiet.length ? h("div", { class: "map-quiet" },
        h("h2", { text: `Gone quiet (${quiet.length})` }),
        h("p", { class: "muted small", text: hiding ? "No answer for a while - left off the map, as in Live." : "Shown on the map." }),
        h("button", { type: "button", class: "btn", text: hiding ? "Show them" : "Hide them",
          onclick: () => { setShowHidden("map", hiding); fillSide(); map?.render(); } })) : null,
    ].filter(Boolean));
  }

  async function loadFleet() {
    let r;
    try { r = await get("/api/map/vehicles"); } catch { return; }      // server restarting: keep the last
    if (!alive) return;
    const byGroup = {};
    for (const v of r.vehicles || []) {
      if (v.group && (!byGroup[v.group] || v.t > byGroup[v.group].t)) byGroup[v.group] = v;
    }
    fleet = { ...r, vehicles: r.vehicles || [], byGroup, byName: Object.fromEntries((r.vehicles || []).map((v) => [v.name, v])) };
    if (!map) return;
    syncForeign();
    map.render();
    fillSide();
  }

  let alive = true;
  let timer = null;
  (async () => {
    let cfg = { tiles: null, background: "none" };
    const [got] = await Promise.allSettled([get("/api/map"), loadFleet()]);
    if (got.status === "fulfilled") cfg = got.value;
    if (!alive) return;
    const site = store.inv.site;
    map = slippyMap({ tiles: cfg.tiles, center: { lat: site.lat, lon: site.lon }, zoom: 15,
      onTilesFailed: () => { extra = "Map images unavailable (no internet?) - positions are by GPS."; notes(); } });
    holder.append(map.el, h("div", { class: "map-buttons" },
      h("button", { type: "button", class: "btn", text: "Fit the site", onclick: () => fitAll() })));
    for (const g of places) makePin(g, false);
    for (const g of vehicles) makePin(g, true);
    syncForeign();
    buildLines();
    colour();
    fillSide();
    extra = cfg.tiles ? "" : "No map background is chosen (Admin → Map) - positions are by GPS.";
    notes();
    map.onRender(draw);
    fitAll();
    if (fleet.enabled) timer = setInterval(loadFleet, FLEET_EVERY_MS);
  })();

  function fitAll() {
    if (!map) return;
    const hide = hideParked();
    const pts = places.filter((g) => !away(g)).map((g) => ({ lat: g.lat, lon: g.lon }));
    for (const g of vehicles) {
      const p = vehiclePos(g);
      if (p && !(hide && parked(g))) pts.push(p);
    }
    for (const v of foreign()) if (v.live || !hide) pts.push(v);
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
    destroy() { alive = false; if (timer) clearInterval(timer); hideTip(); map?.destroy(); },
  };
}
