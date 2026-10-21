// A small hand-made map: Web Mercator tiles (256 px), drag to pan, wheel / +− to zoom, a scale bar.
// Everything placed on it is in lat/lon; `project()` turns that into pixels in the map box.
// Without tiles (no provider chosen, or no internet) the background is a plain grid - the pins
// are still placed by GPS.

import { h, s } from "../dom.js";

const TILE = 256;
const MIN_ZOOM = 3;

const worldX = (lon, z) => ((lon + 180) / 360) * TILE * 2 ** z;
const worldY = (lat, z) => {
  const r = (Math.max(-85, Math.min(85, lat)) * Math.PI) / 180;
  return ((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * TILE * 2 ** z;
};
const lonOf = (x, z) => (x / (TILE * 2 ** z)) * 360 - 180;
const latOf = (y, z) => {
  const n = Math.PI - (2 * Math.PI * y) / (TILE * 2 ** z);
  return (180 / Math.PI) * Math.atan(Math.sinh(n));
};

export function slippyMap({ tiles, center, zoom = 15, onTilesFailed }) {
  const maxZoom = tiles?.max_zoom || 19;
  let lat = center.lat, lon = center.lon, z = zoom;
  let ok = 0, failed = 0, gaveUp = !tiles;
  const imgs = new Map();
  const listeners = new Set();

  const tileLayer = h("div", { class: "map-tiles" });
  const overlay = s("svg", { class: "map-lines", "aria-hidden": "true" });
  const pins = h("div", { class: "map-pins" });
  const scaleBar = h("div", { class: "map-scale" }, h("span"), h("b"));
  const attrib = h("div", { class: "map-attrib", text: tiles?.attribution || "" });
  const zoomIn = h("button", { type: "button", class: "btn", text: "+", "aria-label": "Zoom in", onclick: () => zoomTo(z + 1) });
  const zoomOut = h("button", { type: "button", class: "btn", text: "−", "aria-label": "Zoom out", onclick: () => zoomTo(z - 1) });
  const controls = h("div", { class: "map-controls" }, zoomIn, zoomOut);
  const el = h("div", { class: `map${gaveUp ? " plain" : ""}`, tabindex: "0", "aria-label": "Site map" },
    tileLayer, overlay, pins, controls, scaleBar, attrib);

  function size() { return { w: el.clientWidth || 800, h: el.clientHeight || 500 }; }

  function project(la, lo) {
    const { w, h: hh } = size();
    return { x: worldX(lo, z) - worldX(lon, z) + w / 2, y: worldY(la, z) - worldY(lat, z) + hh / 2 };
  }

  function unproject(x, y) {
    const { w, h: hh } = size();
    return { lat: latOf(worldY(lat, z) + y - hh / 2, z), lon: lonOf(worldX(lon, z) + x - w / 2, z) };
  }

  function tileUrl(tx, ty) {
    const n = 2 ** z;
    return tiles.url.replace("{z}", z).replace("{x}", ((tx % n) + n) % n).replace("{y}", ty);
  }

  function giveUp() {
    if (gaveUp) return;
    gaveUp = true;
    el.classList.add("plain");
    tileLayer.replaceChildren();
    imgs.clear();
    onTilesFailed?.();
  }

  function drawTiles() {
    if (gaveUp) return;
    const { w, h: hh } = size();
    const left = worldX(lon, z) - w / 2, top = worldY(lat, z) - hh / 2;
    const n = 2 ** z;
    const want = new Set();
    for (let tx = Math.floor(left / TILE); tx <= Math.floor((left + w) / TILE); tx++) {
      for (let ty = Math.max(0, Math.floor(top / TILE)); ty <= Math.min(n - 1, Math.floor((top + hh) / TILE)); ty++) {
        const key = `${z}/${tx}/${ty}`;
        want.add(key);
        let img = imgs.get(key);
        if (!img) {
          img = h("img", { alt: "", draggable: "false", referrerpolicy: tiles.referrer || "no-referrer", decoding: "async" });
          img.addEventListener("load", () => { ok++; });
          img.addEventListener("error", () => { failed++; if (!ok && failed >= 4) giveUp(); });
          img.src = tileUrl(tx, ty);
          imgs.set(key, img);
          tileLayer.append(img);
        }
        img.style.transform = `translate(${Math.round(tx * TILE - left)}px, ${Math.round(ty * TILE - top)}px)`;
      }
    }
    for (const [key, img] of imgs) {
      if (!want.has(key)) { img.remove(); imgs.delete(key); }
    }
  }

  function drawScale() {
    const mpp = (156543.03392 * Math.cos((lat * Math.PI) / 180)) / 2 ** z;    // metres per pixel
    const nice = [5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000];
    const m = nice.find((d) => d / mpp >= 70) || nice[nice.length - 1];
    scaleBar.firstChild.style.width = `${Math.round(m / mpp)}px`;
    scaleBar.lastChild.textContent = m >= 1000 ? `${m / 1000} km` : `${m} m`;
  }

  let queued = false;
  function render() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      const { w, h: hh } = size();
      overlay.setAttribute("width", w);
      overlay.setAttribute("height", hh);
      drawTiles();
      drawScale();
      zoomIn.disabled = z >= maxZoom;
      zoomOut.disabled = z <= MIN_ZOOM;
      for (const fn of listeners) fn(project);
    });
  }

  function zoomTo(nz, at) {
    nz = Math.max(MIN_ZOOM, Math.min(maxZoom, Math.round(nz)));
    if (nz === z) return;
    if (at) {                                     // keep the point under the cursor where it is
      const p = unproject(at.x, at.y);
      const { w, h: hh } = size();
      z = nz;
      lat = latOf(worldY(p.lat, z) - (at.y - hh / 2), z);
      lon = lonOf(worldX(p.lon, z) - (at.x - w / 2), z);
    } else {
      z = nz;
    }
    for (const img of imgs.values()) img.remove();
    imgs.clear();
    render();
  }

  function fit(points, pad = 60) {
    if (!points.length) return;
    const { w, h: hh } = size();
    const la = points.map((p) => p.lat), lo = points.map((p) => p.lon);
    const c = { lat: (Math.min(...la) + Math.max(...la)) / 2, lon: (Math.min(...lo) + Math.max(...lo)) / 2 };
    let nz = maxZoom;
    while (nz > MIN_ZOOM) {
      const dx = worldX(Math.max(...lo), nz) - worldX(Math.min(...lo), nz);
      const dy = worldY(Math.min(...la), nz) - worldY(Math.max(...la), nz);
      if (dx <= w - 2 * pad && dy <= hh - 2 * pad) break;
      nz--;
    }
    lat = c.lat; lon = c.lon;
    for (const img of imgs.values()) img.remove();
    imgs.clear();
    z = Math.min(nz, 18);
    render();
  }

  // -- dragging and the wheel ------------------------------------------------
  let drag = null;
  el.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.target.closest("button")) return;
    drag = { x: e.clientX, y: e.clientY, wx: worldX(lon, z), wy: worldY(lat, z) };
    el.setPointerCapture(e.pointerId);
    el.classList.add("dragging");
  });
  el.addEventListener("pointermove", (e) => {
    if (!drag) return;
    lon = lonOf(drag.wx - (e.clientX - drag.x), z);
    lat = latOf(drag.wy - (e.clientY - drag.y), z);
    render();
  });
  const endDrag = () => { drag = null; el.classList.remove("dragging"); };
  el.addEventListener("pointerup", endDrag);
  el.addEventListener("pointercancel", endDrag);
  let wheelAt = 0;
  el.addEventListener("wheel", (e) => {
    e.preventDefault();
    const now = performance.now();
    if (now - wheelAt < 180) return;               // one zoom step per wheel "notch" burst
    wheelAt = now;
    const r = el.getBoundingClientRect();
    zoomTo(z + (e.deltaY < 0 ? 1 : -1), { x: e.clientX - r.left, y: e.clientY - r.top });
  }, { passive: false });
  el.addEventListener("keydown", (e) => {
    const step = 80;
    const moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (e.key === "+" || e.key === "=") zoomTo(z + 1);
    else if (e.key === "-") zoomTo(z - 1);
    else if (moves[e.key]) {
      e.preventDefault();
      const [dx, dy] = moves[e.key];
      lon = lonOf(worldX(lon, z) + dx, z);
      lat = latOf(worldY(lat, z) + dy, z);
      render();
    }
  });
  const ro = new ResizeObserver(() => render());
  ro.observe(el);

  return {
    el, pins, overlay, project, fit, render,
    onRender(fn) { listeners.add(fn); render(); },
    get plain() { return gaveUp; },
    destroy() { ro.disconnect(); listeners.clear(); },
  };
}
