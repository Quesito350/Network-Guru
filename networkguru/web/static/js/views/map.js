// Map page: a pin per located site location (trailers, towers, buildings, ...) coloured by its live
// status, every vehicle where its GPS last put it, and the 60 GHz links as lines. Hover for the
// reason, click for the location's or vehicle's page. Positions come from the Groups sheet (Lat / Lon,
// filled from the Rajant snapshot); vehicles from the Mobius InfluxDB (0.9.3: /api/map/vehicles - every
// Mobius vehicle, its last position kept across restarts), else their GNSS component (the simulator),
// else lat / lon fields Mobius has for them (0.8.0). A parked vehicle stays at its last position, faded
// ("Hide them" takes them off); a Mobius vehicle that is not in the inventory gets a grey pin with its
// Mobius name. Locations that have gone quiet are left out, as in Live (quiet.js).
// 0.9.5: while the page is open the server reads the positions every few seconds (`every_s`); a vehicle
// glides from where it is shown to each new position, its arrow points the way it drove (`heading`), and a
// live one draws a fading trail of its last 2 minutes. Only the vehicles move on each frame - not the tiles.
// 0.9.6: layers - the Layers button (top left) switches each kind of place, running / parked / grey
// vehicles, the 60 GHz links, the trails and the names on or off; this browser remembers the choice.
// 0.9.7: hover shows what Mobius says a vehicle is (type, model, IP); the Mobius simulator's vehicles are a
// layer of their own, off unless switched on.
// 0.9.7: "Fit the site" (and the first view) fits the site area - around the located places - so a vehicle
// whose last GPS fix is far away (a glitch) no longer zooms the map out; the side panel lists those.
// 0.9.9: `embedded` - the Overview's map: the same pins, links, vehicles, Layers and Fit the site, without the page
// title and the side list. The map is always the live state, so the Map page is always Live (app.js).

import { get } from "../api.js";
import { h, s, setClass, setText } from "../dom.js";
import { fmtAgo, fmtWhen, STATUS_LABEL, statusName } from "../fmt.js";
import { isQuiet, setShowHidden, showHidden } from "../quiet.js";
import { dot } from "../status.js";
import { node, store } from "../store.js";
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
const FLEET_EVERY_MS = 3000;             // unless the server says otherwise (every_s)
const GLIDE_MAX_M = 200;                 // a longer jump (GPS back after a gap) moves at once
const TURN_M = 2;                        // the simulator's vehicles: moved this far = a new heading
const SITE_MIN_KM = 2;                   // the site area: at least this far around the places' middle

const glyphOf = (kind) => GLYPH[kind] || (kind === "server" || kind === "client" ? GLYPH.core : GLYPH.other);
const located = (g) => typeof g.lat === "number" && typeof g.lon === "number";

function lsGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } }

// the layers this viewer switched off (0.9.6); "Hide them" before 0.9.6 was the parked layer
const LAYERS_KEY = "ng_map_layers";
const PLACE_TEXT = { trailer: "Trailers", tower: "Towers", building: "Buildings", repeater: "Repeaters", cow: "COW",
  core: "Network core", server: "Servers", client: "Desks", other: "Other places" };
const placeLayer = (g) => `place:${PLACE_TEXT[g.kind] ? g.kind : "other"}`;

const DEFAULT_OFF = new Set(["simulated"]);           // layers that are off until switched on

function layersSaved() {
  try {
    const x = JSON.parse(lsGet(LAYERS_KEY) || "null");
    if (x && Array.isArray(x.off)) return { off: new Set(x.off.map(String)), on: new Set((x.on || []).map(String)) };
  } catch { /* unreadable: the defaults */ }
  return { off: new Set(lsGet("ng_map_hide_parked") === "1" ? ["parked"] : []), on: new Set() };
}
const calm = () => { try { return matchMedia("(prefers-reduced-motion: reduce)").matches; } catch { return false; } };

// (east, north) in metres from a to b - flat earth, fine over a site
function metres(a, b) {
  return [(b.lon - a.lon) * 111320 * Math.cos(((a.lat + b.lat) / 2) * Math.PI / 180), (b.lat - a.lat) * 110540];
}

export function mapView(root, nav, { embedded = false } = {}) {
  const note = h("div", { class: "map-note hidden", role: "status" });
  const side = h("aside", { class: "map-side panel pad" });
  const holder = h("div", { class: "map-holder" });
  if (embedded) root.append(note, holder);
  else {
    root.append(h("div", { class: "page-head" }, h("h1", { text: "Map" }), h("span", { class: "sub", text: store.inv.site.name })),
      note, h("div", { class: "map-layout" }, holder, side));
  }

  const groups = Object.values(store.groups);
  const vehicles = groups.filter((g) => g.tab === "vehicles");
  const places = groups.filter((g) => g.tab !== "vehicles" && located(g));
  const median = (xs) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)];
  const site = store.inv.site;
  const centre = places.length ? { lat: median(places.map((g) => g.lat)), lon: median(places.map((g) => g.lon)) }
    : typeof site.lat === "number" && typeof site.lon === "number" ? { lat: site.lat, lon: site.lon } : null;
  const kmFrom = (p) => (centre ? Math.hypot(...metres(centre, p)) / 1000 : 0);
  const siteKm = Math.max(SITE_MIN_KM, 1.5 * Math.max(0, ...places.map(kmFrom)));
  const near = (p) => kmFrom(p) <= siteKm;
  let map = null;
  let extra = "";                  // why the background is plain, when it is
  const pinOf = {};                // group id -> { btn }
  const foreignPin = {};           // Mobius name -> button (in Mobius, not in the inventory)
  const lines = [];                // { a, b, comps, el }
  let fleet = { enabled: false, vehicles: [], byGroup: {}, byName: {} };
  let everyMs = FLEET_EVERY_MS;
  const glides = {};               // "g:<group>" / "n:<Mobius name>" -> { from, to, t0, dur, heading }
  const trails = {};               // the same keys -> { el (svg g), pts: [[lat, lon], ...] }
  let raf = 0;
  const { off: hiddenLayers, on: shownLayers } = layersSaved();
  let closeLayers = null;
  const isOn = (layer) => (DEFAULT_OFF.has(layer) ? shownLayers.has(layer) : !hiddenLayers.has(layer));
  const hideParked = () => hiddenLayers.has("parked");
  const away = (g) => (isQuiet(g, { always: true }) && !showHidden("map")) || !isOn(placeLayer(g));

  function setLayer(layer, on) {
    const set = DEFAULT_OFF.has(layer) ? shownLayers : hiddenLayers;
    if (on === DEFAULT_OFF.has(layer)) set.add(layer);
    else set.delete(layer);
    lsSet(LAYERS_KEY, JSON.stringify({ off: [...hiddenLayers], on: [...shownLayers] }));
    map?.el.classList.toggle("no-labels", hiddenLayers.has("labels"));
    map?.render();
    fillSide();
  }

  // Where a vehicle is drawn now: gliding from where it was shown to `target` over one fetch interval.
  function shown(key, target) {
    if (!target) return null;
    const now = performance.now();
    let gl = glides[key];
    if (!gl || gl.to.lat !== target.lat || gl.to.lon !== target.lon) {
      const from = gl ? at(gl, now) : null;
      const [e, n] = from ? metres(from, target) : [0, 0];
      const far = Math.hypot(e, n) > GLIDE_MAX_M;
      const heading = Math.hypot(e, n) >= TURN_M && !far ? (Math.atan2(e, n) * 180 / Math.PI + 360) % 360 : gl?.heading ?? null;
      gl = glides[key] = { from: from && !far && !calm() ? from : { lat: target.lat, lon: target.lon },
        to: { lat: target.lat, lon: target.lon }, t0: now, dur: everyMs, heading };
      kick();
    }
    return at(gl, now);
  }
  function at(gl, now) {
    const k = Math.min(1, Math.max(0, (now - gl.t0) / gl.dur));
    return { lat: gl.from.lat + (gl.to.lat - gl.from.lat) * k, lon: gl.from.lon + (gl.to.lon - gl.from.lon) * k };
  }
  const gliding = (now) => Object.values(glides).some((gl) => now - gl.t0 < gl.dur
    && (gl.from.lat !== gl.to.lat || gl.from.lon !== gl.to.lon));
  function kick() { if (!raf && map) raf = requestAnimationFrame(frame); }
  function frame(now) {
    raf = 0;
    if (!alive || !map) return;
    drawMoving(map.project);
    if (gliding(now)) kick();
  }

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
      isVehicle ? idRow(fleet.byGroup[g.id]) : null,
      isVehicle ? posRow(vehiclePos(g)) : null].filter(Boolean));
  }

  // what Mobius says a vehicle is (0.9.7)
  const idRow = (v) => (v && (v.type || v.model || v.ip)
    ? { value: [v.type, v.model].filter(Boolean).join(" · ") || "In Mobius", label: v.ip || "" } : null);

  function foreignTip(e, name) {
    const v = fleet.byName[name];
    showTip(e.clientX, e.clientY, name, [
      v?.simulated ? { value: "Mobius simulator", label: "not a real vehicle" } : { value: "In Mobius", label: "not a vehicle here yet" },
      { value: v?.live ? "Talking to Mobius" : "Not talking to Mobius", label: "" },
      idRow(v),
      posRow(v),
      v?.simulated ? null : { value: "", label: "It gets a card of its own within a minute (added from Mobius)" }].filter(Boolean));
  }

  const tipHandlers = (show) => ({
    onmouseenter: show, onmousemove: show, onmouseleave: hideTip,
    onfocus: (e) => { const r = e.target.getBoundingClientRect(); show({ clientX: r.right, clientY: r.top }); },
    onblur: hideTip,
  });

  function turn(btn, deg) {
    btn.firstChild.style.transform = deg == null ? "" : `rotate(${Math.round(deg)}deg)`;
  }

  // a fading line behind each live vehicle: its last 2 minutes (the server's trail)
  function syncTrails() {
    const want = {};
    for (const g of vehicles) {
      const f = fleet.byGroup[g.id];
      if (f?.trail?.length > 1) want[`g:${g.id}`] = f.trail;
    }
    for (const v of foreign()) if (v.trail?.length > 1) want[`n:${v.name}`] = v.trail;
    for (const [key, tr] of Object.entries(trails)) {
      if (!want[key]) { tr.el.remove(); delete trails[key]; }
    }
    for (const [key, pts] of Object.entries(want)) {
      const tr = trails[key] || (trails[key] = { el: s("g", { class: "map-trail" }), pts: [] });
      if (!tr.el.isConnected) map.overlay.prepend(tr.el);
      tr.pts = pts;
      while (tr.el.childNodes.length < pts.length - 1) tr.el.append(s("line", {}));
      while (tr.el.childNodes.length > pts.length - 1) tr.el.lastChild.remove();
      const n = tr.el.childNodes.length;
      tr.el.childNodes.forEach((ln, i) => ln.setAttribute("stroke-opacity", (0.15 + 0.7 * ((i + 1) / n)).toFixed(2)));
    }
  }

  function drawTrail(key, project, pos, hidden) {
    const tr = trails[key];
    if (!tr) return;
    tr.el.classList.toggle("hidden", hidden || !pos);
    if (hidden || !pos) return;
    // the newest point is where the pin glides to: the last segment ends at the pin instead
    const pts = tr.pts.slice(0, -1).map(([la, lo]) => project(la, lo));
    pts.push(project(pos.lat, pos.lon));
    tr.el.childNodes.forEach((ln, i) => {
      ln.setAttribute("x1", pts[i].x); ln.setAttribute("y1", pts[i].y);
      ln.setAttribute("x2", pts[i + 1].x); ln.setAttribute("y2", pts[i + 1].y);
    });
  }

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

  // the vehicles only - every animation frame while one glides
  function drawMoving(project) {
    const hide = hideParked();
    for (const g of vehicles) {
      const pin = pinOf[g.id];
      if (!pin) continue;
      const key = `g:${g.id}`;
      const pos = shown(key, vehiclePos(g));
      const off = !!pos && parked(g);
      const hidden = !pos || (off ? hide : !isOn("running"));
      pin.btn.classList.toggle("hidden", hidden);
      pin.btn.classList.toggle("parked", off);
      if (pos) {
        const { x, y } = project(pos.lat, pos.lon);
        pin.btn.style.transform = `translate(${x}px, ${y}px)`;
        turn(pin.btn, fleet.byGroup[g.id]?.heading ?? glides[key]?.heading);
      }
      drawTrail(key, project, pos, hidden || off || !isOn("trails"));
    }
    for (const [name, btn] of Object.entries(foreignPin)) {
      const v = fleet.byName[name];
      if (!v) continue;
      const key = `n:${name}`;
      const pos = shown(key, v);
      const hid = (v.simulated ? !isOn("simulated") : !isOn("foreign")) || (!v.live && hide);
      btn.classList.toggle("parked", !v.live);
      btn.classList.toggle("hidden", hid);
      const { x, y } = project(pos.lat, pos.lon);
      btn.style.transform = `translate(${x}px, ${y}px)`;
      turn(btn, v.heading);
      drawTrail(key, project, pos, !v.live || hid || !isOn("trails"));
    }
  }

  function draw(project) {
    for (const g of places) {
      const { x, y } = project(g.lat, g.lon);
      pinOf[g.id].btn.style.transform = `translate(${x}px, ${y}px)`;
      pinOf[g.id].btn.classList.toggle("hidden", away(g));
    }
    drawMoving(project);
    for (const l of lines) {
      l.el.classList.toggle("hidden", !isOn("links") || away(l.a) || away(l.b));
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
    if (embedded) return;                        // the Overview's map has no side list
    const quiet = places.filter((g) => isQuiet(g, { always: true }));
    const hiding = !showHidden("map");
    const others = foreign();
    const parkedOnes = [...vehicles.filter((g) => vehiclePos(g) && parked(g)), ...others.filter((v) => !v.live)];
    const hideP = hideParked();
    const missing = groups.filter((g) => (g.tab === "vehicles" ? !vehiclePos(g) : !located(g))
      && !(g.tab !== "vehicles" && hiding && isQuiet(g, { always: true })));
    const now = Date.now() / 1000;
    const far = farAway();
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
          onclick: () => setLayer("parked", hideP) })) : null,
      others.length ? h("div", { class: "map-foreign" },
        h("h2", { text: `In Mobius, not in the inventory (${others.length})` }),
        h("ul", { class: "plain" }, ...others.map((v) =>
          h("li", {}, dot(v.live ? 1 : 2), h("span", { text: v.simulated ? `${v.name} (simulator)` : v.name }),
            h("span", { class: "muted", text: v.live ? "now" : fmtWhen(v.t, now) })))),
        h("p", { class: "muted small", text: "A real vehicle gets its own card, pin and checks by itself within a minute "
          + "(added from Mobius). The Mobius simulator's vehicles stay here - their map layer is off unless you switch it on." })) : null,
      far.length ? h("div", { class: "map-far" },
        h("h2", { text: `Far from the site (${far.length})` }),
        h("ul", { class: "plain" }, ...far.map((x) => h("li", {}, x.g ? goTo(x.g) : h("span", { text: x.name }),
          h("span", { class: "muted", text: `${kmText(x.km)} away${x.t ? ` · ${fmtWhen(x.t, now)}` : ""}` })))),
        h("p", { class: "muted small", text: "Their last GPS position is outside the site, so Fit the site leaves them out "
          + "- often a bad GPS fix. They are still on the map if you pan to them." })) : null,
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
    if (r.every_s > 0) everyMs = r.every_s * 1000;
    if (!map) return;
    syncForeign();
    syncTrails();
    drawMoving(map.project);
    sideSoon();
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
    map.el.append(layersControl());
    map.el.classList.toggle("no-labels", hiddenLayers.has("labels"));
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
    syncTrails();
    // a hidden tab stops asking, so the server stops its quick position reads too
    if (fleet.enabled) timer = setInterval(() => { if (!document.hidden) loadFleet(); }, everyMs);
  })();

  function fitAll() {
    if (!map) return;
    const hide = hideParked();
    const pts = places.filter((g) => !away(g)).map((g) => ({ lat: g.lat, lon: g.lon }));
    for (const g of vehicles) {
      const p = vehiclePos(g);
      if (p && (parked(g) ? !hide : isOn("running"))) pts.push(p);
    }
    for (const v of foreign()) if ((v.simulated ? isOn("simulated") : isOn("foreign")) && (v.live || !hide)) pts.push(v);
    const here = pts.filter(near);                  // far-away fixes (a GPS glitch) never zoom the site out
    if (here.length || pts.length) map.fit(here.length ? here : pts);
  }

  // vehicles whose last position is outside the site area: [{ name, km, t, g }]
  function farAway() {
    const out = [];
    for (const g of vehicles) {
      const p = vehiclePos(g);
      if (p && !near(p)) out.push({ name: g.name, km: kmFrom(p), t: p.t, g });
    }
    for (const v of foreign()) if (!near(v)) out.push({ name: v.name, km: kmFrom(v), t: v.t, g: null });
    return out.sort((a, b) => b.km - a.km);
  }
  const kmText = (km) => (km >= 10 ? `${Math.round(km).toLocaleString()} km` : `${km.toFixed(1)} km`);

  // the Layers button and its panel, top left on the map (0.9.6)
  function layersControl() {
    const panel = h("div", { class: "map-layers-panel hidden", role: "group", "aria-label": "Map layers" });
    const btn = h("button", { type: "button", class: "btn", text: "Layers", "aria-expanded": "false", onclick: () => toggle() });
    const box = h("div", { class: "map-layers", onpointerdown: (e) => e.stopPropagation(), onwheel: (e) => e.stopPropagation(),
      onkeydown: (e) => { if (e.key !== "Escape") e.stopPropagation(); } }, btn, panel);
    const outside = (e) => { if (!box.contains(e.target)) toggle(false); };
    const esc = (e) => { if (e.key === "Escape") { toggle(false); btn.focus(); } };
    function toggle(open = panel.classList.contains("hidden")) {
      panel.classList.toggle("hidden", !open);
      btn.setAttribute("aria-expanded", String(open));
      if (open) {
        fill();
        document.addEventListener("pointerdown", outside);
        document.addEventListener("keydown", esc);
      } else {
        document.removeEventListener("pointerdown", outside);
        document.removeEventListener("keydown", esc);
      }
    }
    closeLayers = () => toggle(false);
    const row = (layer, label, count) => {
      const cb = h("input", { type: "checkbox", checked: isOn(layer), onchange: () => setLayer(layer, cb.checked) });
      return h("label", { class: "check" }, cb, ` ${label}`, count == null ? null : h("span", { class: "muted", text: ` ${count}` }));
    };
    function fill() {
      const kinds = {};
      for (const g of places) kinds[placeLayer(g)] = (kinds[placeLayer(g)] || 0) + 1;
      const others = foreign();
      const running = vehicles.filter((g) => vehiclePos(g) && !parked(g)).length;
      const parkedN = vehicles.filter((g) => vehiclePos(g) && parked(g)).length + others.filter((v) => !v.live).length;
      panel.replaceChildren(...[
        Object.keys(kinds).length ? h("h3", { text: "Places" }) : null,
        ...Object.keys(kinds).sort().map((k) => row(k, PLACE_TEXT[k.slice(6)], kinds[k])),
        h("h3", { text: "Vehicles" }),
        row("running", "Running", running),
        row("parked", "Parked (faded)", parkedN),
        fleet.enabled ? row("foreign", "Not in the inventory (grey)", others.filter((v) => !v.simulated).length) : null,
        others.some((v) => v.simulated) ? row("simulated", "Mobius simulator", others.filter((v) => v.simulated).length) : null,
        h("h3", { text: "Lines and names" }),
        lines.length ? row("links", "60 GHz links", lines.length) : null,
        row("trails", "Vehicle trails (2 min)"),
        row("labels", "Names"),
        hiddenLayers.size ? h("button", { type: "button", class: "linkish", text: "Show all", onclick: () => {
          for (const l of [...hiddenLayers]) setLayer(l, true);
          fill();
        } }) : null,                                  // ("Show all" leaves the simulator's vehicles as they are)
      ].filter(Boolean));
    }
    return box;
  }

  let lastSide = 0;
  function sideSoon() {
    const now = Date.now();
    if (now - lastSide > 5000) { lastSide = now; fillSide(); }
  }
  return {
    update() {
      if (!map) return;
      colour();
      map.render();
      sideSoon();
    },
    refreshRange() {},
    destroy() {
      alive = false;
      if (timer) clearInterval(timer);
      if (raf) cancelAnimationFrame(raf);
      closeLayers?.();
      hideTip();
      map?.destroy();
    },
  };
}
