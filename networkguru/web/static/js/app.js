// Bootstrap: header (Live/History, range), tabs (Overview first - the start page), routing, events
// drawer, help, What's new, and the update banner.
// 0.9.9: the Errors button (next to Map) and its drawer, toasts for new problems (toasts.js), the Overview's
// full-screen "wall" layout (also #/overview?full=1 for a TV), and the Map always in Live (it has no History).

import { connectLive, get } from "./api.js";
import { clear, h, s, setText } from "./dom.js";
import { fmtAgo, fromLocalInput, setup, STATUS_LABEL, toLocalInput, tz } from "./fmt.js";
import { badge, countsLine, dot } from "./status.js";
import { modal } from "./modal.js";
import { feedbackButton } from "./feedback.js";
import { errorButton, watchErrors } from "./problem.js";
import { startMobiusSummary } from "./quiet.js";
import { indexInventory, node, notify, store, subscribe, window_ } from "./store.js";
import { problemRows, startToasts } from "./toasts.js";
import { initUpdates } from "./updates.js";
import { adminView } from "./views/admin.js";
import { eventLink, fillAttention, TAB_TITLE } from "./views/common.js";
import { componentView, groupView } from "./views/detail.js";
import { homeView } from "./views/home.js";
import { mapView } from "./views/map.js";
import { overviewView } from "./views/overview.js";
import { reportView } from "./views/report.js";
import { hideTip } from "./widgets/chart.js";

const TABS = ["vehicles", "control", "infrastructure"];
const PAGES = ["overview", "admin", "report", "map"];
let current = null;
let route = { parts: [], key: "" };

function lsGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } }

// ---------------------------------------------------------------------------
// routing: #/overview, #/vehicles, #/vehicles/<id>, #/c/<component>, #/admin, #/report, #/map,
// + ?mode=history&range=7d | &from=&to=
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const q = new URLSearchParams(query || "");
  store.mode = q.get("mode") === "history" && parts[0] !== "map" ? "history" : "live";    // the map is live only
  if (q.get("from") && q.get("to")) {
    store.range = "custom";
    store.from = Number(q.get("from"));
    store.to = Number(q.get("to"));
  } else {
    store.range = ["1h", "24h", "7d", "30d"].includes(q.get("range")) ? q.get("range") : "24h";
  }
  if (!parts.length || (!TABS.includes(parts[0]) && !PAGES.includes(parts[0]) && parts[0] !== "c")) parts.splice(0, parts.length, "overview");
  return { parts, key: parts.join("/") + "?" + (query || "") };
}

function queryString() {
  if (store.mode !== "history") return "";
  if (store.range === "custom") return `?mode=history&from=${Math.round(store.from)}&to=${Math.round(store.to)}`;
  return `?mode=history&range=${store.range}`;
}

export function nav(href) {
  const target = href.split("?")[0] === "#/map" ? "#/map" : href.includes("?") ? href : href + queryString();
  if (location.hash === target) render(true);
  else location.hash = target;
}

function setTimeQuery() {
  const [path, query] = location.hash.split("?");
  const show = new URLSearchParams(query || "").get("show");      // keep a tile filter across Live / History
  const qs = queryString();
  location.hash = (path || "#/overview") + (show ? (qs ? `${qs}&show=${show}` : `?show=${show}`) : qs);
}

function render(force = false) {
  const r = parseHash();
  if (!force && r.key === route.key && current) return;
  route = r;
  hideTip();
  current?.destroy?.();
  const main = document.getElementById("main");
  clear(main);
  const [a, b] = r.parts;
  const full = new URLSearchParams(r.key.split("?")[1] || "").get("full") === "1";
  if (a === "overview" && full) enterWall(false);
  else if (a !== "overview" && document.body.classList.contains("wall")) exitWall();
  if (a === "overview") current = homeView(main, nav, { full: () => enterWall(true) });
  else if (a === "admin") current = adminView(main, nav);
  else if (a === "report") current = reportView(main, nav);
  else if (a === "map") current = mapView(main, nav);
  else if (a === "c") current = componentView(main, b, nav);
  else if (b) current = groupView(main, b, nav);
  else current = overviewView(main, a, nav);
  updateHeader();
  updateTabs();
  window.scrollTo(0, 0);
  document.title = `${titleFor(r.parts)} · ${store.meta.site.name}`;
}

function titleFor(parts) {
  if (parts[0] === "overview") return "Overview";
  if (parts[0] === "admin") return "Admin";
  if (parts[0] === "report") return "Morning report";
  if (parts[0] === "map") return "Map";
  if (parts[0] === "c") return store.comps[parts[1]]?.name || "Component";
  if (parts[1]) return store.groups[parts[1]]?.name || "Detail";
  return TAB_TITLE[parts[0]];
}

// ---------------------------------------------------------------------------
// header
const el = {};

function buildHeader() {
  const bar = document.getElementById("topbar");
  el.mode = h("div", { class: "seg", role: "group", "aria-label": "Live or history" },
    ...["live", "history"].map((m) => h("button", { type: "button", "data-mode": m, text: m === "live" ? "Live" : "History",
      onclick: () => { store.mode = m; if (m === "history" && !store.range) store.range = "24h"; setTimeQuery(); } })));
  el.range = h("div", { class: "seg", role: "group", "aria-label": "Time range" },
    ...["1h", "24h", "7d", "30d", "custom"].map((r) => h("button", { type: "button", "data-range": r, text: r === "custom" ? "Custom" : r,
      onclick: () => {
        if (r === "custom") { el.custom.classList.remove("hidden"); return; }
        store.range = r; setTimeQuery();
      } })));
  const now = Date.now() / 1000;
  el.cFrom = h("input", { type: "datetime-local", "aria-label": "From", value: toLocalInput(now - 86400) });
  el.cTo = h("input", { type: "datetime-local", "aria-label": "To", value: toLocalInput(now) });
  el.custom = h("div", { class: "custom-range hidden" }, el.cFrom, "to", el.cTo,
    h("button", { type: "button", text: "Apply", onclick: () => {
      const f = fromLocalInput(el.cFrom.value), t = fromLocalInput(el.cTo.value);
      if (!(f < t)) return alert("'From' must be before 'To'.");
      if (t - f > 31 * 86400) return alert("The longest range is 31 days.");
      store.range = "custom"; store.from = f; store.to = t; setTimeQuery();
    } }));
  el.conn = h("span", { class: "conn connecting" }, h("span", { class: "dot" }), h("span", { class: "txt", text: "Connecting…" }));
  el.evCount = h("span", { class: "count hidden" });
  el.evBtn = h("button", { class: "hbtn", type: "button", onclick: openEvents }, "Events", el.evCount);
  el.theme = h("button", { class: "hbtn", type: "button", onclick: cycleTheme, title: "Light / dark display" });
  el.help = h("button", { class: "hbtn", type: "button", text: "?", title: "How to read this dashboard", "aria-label": "Help", onclick: openHelp });
  el.admin = h("a", { class: "hbtn", href: "#/admin", text: "Admin", title: "Updates and the morning report (sign-in needed to change anything)" });
  el.report = errorButton();
  el.feedback = feedbackButton();
  bar.append(
    h("a", { class: "brand", href: "#/overview" }, h("img", { src: "/static/img/asi_logo_white.png", alt: "ASI" }),
      h("div", {}, h("div", { class: "title", text: "Network Guru" }), h("div", { class: "site", text: store.meta.site.name }))),
    el.mode, el.range, el.custom, h("span", { class: "spacer" }), el.conn, el.evBtn, el.feedback, el.report, el.admin, el.theme, el.help);
  applyTheme(lsGet("ng_theme") || "auto");
}

function updateHeader() {
  const onMap = route.parts[0] === "map";            // the map is always the live state: no History there
  el.mode.classList.toggle("hidden", onMap);
  for (const b of el.mode.children) b.setAttribute("aria-pressed", String(b.dataset.mode === store.mode));
  el.range.classList.toggle("hidden", onMap || store.mode !== "history");
  for (const b of el.range.children) b.setAttribute("aria-pressed", String(b.dataset.range === store.range));
  el.custom.classList.toggle("hidden", onMap || !(store.mode === "history" && store.range === "custom"));
  if (store.range === "custom" && store.from && store.to) {
    el.cFrom.value = toLocalInput(store.from);
    el.cTo.value = toLocalInput(store.to);
  }
}

function tickConn() {
  const txt = el.conn.querySelector(".txt");
  el.conn.className = `conn ${store.ws === "connected" ? "" : store.ws}`;
  if (store.ws === "connected") {
    const age = store.lastMsg ? Date.now() / 1000 - store.lastMsg : null;
    setText(txt, `Live · updated ${fmtAgo(age)}`);
  } else setText(txt, store.ws === "down" ? "Disconnected - retrying…" : "Connecting…");
}

function applyTheme(mode) {
  if (mode === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", mode);
  setText(el.theme, { auto: "Auto", light: "Light", dark: "Dark" }[mode]);
  el.theme.dataset.mode = mode;
}
function cycleTheme() {
  const next = { auto: "dark", dark: "light", light: "auto" }[el.theme.dataset.mode || "auto"];
  lsSet("ng_theme", next);
  applyTheme(next);
}

// ---------------------------------------------------------------------------
function buildTabs() {
  const nav_ = document.getElementById("tabs");
  el.tabs = {};
  const hc = h("span", { class: "tcounts" });
  el.home = { a: h("a", { href: "#/overview" }, dot(1), "Overview", hc), counts: hc };
  el.home.a.addEventListener("click", (e) => { e.preventDefault(); nav("#/overview"); });
  nav_.append(el.home.a);
  for (const t of TABS) {
    const counts = h("span", { class: "tcounts" });
    const a = h("a", { href: `#/${t}` }, dot(1), TAB_TITLE[t], counts);
    a.addEventListener("click", (e) => { e.preventDefault(); nav(`#/${t}`); });
    el.tabs[t] = { a, counts };
    nav_.append(a);
  }
  el.mapTab = h("a", { href: "#/map", class: "tab-map" },
    s("svg", { viewBox: "0 0 16 16", "aria-hidden": "true" },
      s("path", { d: "M8 1.5a4.5 4.5 0 00-4.5 4.5c0 3.4 4.5 8.5 4.5 8.5s4.5-5.1 4.5-8.5A4.5 4.5 0 008 1.5zm0 6.3a1.8 1.8 0 110-3.6 1.8 1.8 0 010 3.6z" })),
    "Map");
  el.mapTab.addEventListener("click", (e) => { e.preventDefault(); nav("#/map"); });
  el.errCount = h("span", { class: "ecount" });
  el.errTab = h("a", { href: "#", class: "tab-errors", role: "button", title: "Everything that is Warning or Critical now" },
    s("svg", { viewBox: "0 0 16 16", "aria-hidden": "true" }, s("path", { d: "M8 1.2l7 12.6H1z" })), "Errors", el.errCount);
  el.errTab.addEventListener("click", (e) => { e.preventDefault(); openErrors(); });
  nav_.append(el.errTab, el.mapTab);
}

// the Errors count: the tabs row and the full-screen bar, coloured by the worst
function updateErrors() {
  const rows = problemRows();
  const worst = rows.length ? Math.max(...rows.map((r) => r.n.s)) : 0;
  const cls = `ecount${worst >= 4 ? " s-critical" : worst >= 3 ? " s-warning" : ""}`;
  for (const c of [el.errCount, el.wallCount]) {
    if (!c) continue;
    c.className = cls;
    setText(c, String(rows.length));
  }
  const t = rows.length ? `${rows.length} need attention now` : "Nothing needs attention now";
  el.errTab.title = t;
  if (el.wallErr) el.wallErr.title = t;
  if (errDrawer?.open) errDrawer.fill(rows);
}

// the Errors drawer: the Overview's old "Needs attention now" list, for all three tabs, kept up to date
let errDrawer = null;
function openErrors() {
  if (!errDrawer) {
    const list = h("ul");
    const scrim = h("div", { class: "scrim hidden" });
    const close = () => { panel.classList.remove("open"); scrim.classList.add("hidden"); errDrawer.open = false; };
    const panel = h("aside", { class: "drawer errors-drawer", "aria-label": "Errors" },
      h("header", {}, h("h2", { text: "Errors · now" }),
        h("button", { class: "xbtn", type: "button", "aria-label": "Close", text: "×", onclick: () => close() })),
      h("p", { class: "muted small drawer-note", text: "Every device and place that is Warning or Critical now, worst first. "
        + "Devices behind a failed one, and places gone quiet, are left out." }),
      h("div", { class: "issues" }, list));
    scrim.addEventListener("click", close);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && errDrawer.open) close(); });
    document.body.append(scrim, panel);
    errDrawer = { panel, scrim, open: false,
      fill(rows = problemRows()) { fillAttention(list, rows, (href) => { close(); nav(href); }, 500); } };
  }
  errDrawer.open = true;
  errDrawer.scrim.classList.remove("hidden");
  errDrawer.panel.classList.add("open");
  errDrawer.fill();
}

// ---------------------------------------------------------------------------
// the Overview on the whole screen ("wall"): no header, tabs or footer; the panels and the map fill it
let wallFs = false;                  // we asked the browser for full screen
function buildWallBar() {
  el.wallCount = h("span", { class: "ecount" });
  el.wallErr = h("button", { type: "button", class: "btn", onclick: () => openErrors() }, "Errors", el.wallCount);
  document.body.append(h("div", { class: "wall-bar" }, el.wallErr,
    h("button", { type: "button", class: "btn", text: "Exit full screen", onclick: () => exitWall() })));
  document.addEventListener("fullscreenchange", () => {
    if (!document.fullscreenElement && wallFs) { wallFs = false; exitWall(); }
  });
  document.addEventListener("keydown", (e) => {          // without the browser's full screen (?full=1), Esc leaves
    if (e.key === "Escape" && document.body.classList.contains("wall") && !document.querySelector(".drawer.open")) exitWall();
  });
}

function enterWall(ask) {
  document.body.classList.add("wall");
  const de = document.documentElement;
  if (ask && de.requestFullscreen && !document.fullscreenElement) {
    de.requestFullscreen().then(() => { wallFs = true; }).catch(() => { /* refused: the layout alone */ });
  }
  updateErrors();
}

function exitWall() {
  document.body.classList.remove("wall");
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  wallFs = false;
  if (/[?&]full=1/.test(location.hash)) location.hash = "#/overview";
}

function updateTabs() {
  el.mapTab?.setAttribute("aria-current", route.parts[0] === "map" ? "page" : "false");
  if (el.home) {                      // the Overview: the worst of the three tabs, their counts added up
    const worst = Math.max(...TABS.map((t) => node(t).s));
    const sum = {};
    for (const t of TABS) for (const [k, v] of Object.entries(node(t).counts || {})) if (k !== "ok") sum[k] = (sum[k] || 0) + v;
    el.home.a.setAttribute("aria-current", route.parts[0] === "overview" ? "page" : "false");
    el.home.a.replaceChild(dot(worst, `Overview: ${STATUS_LABEL[["ok", "unknown", "offline", "warning", "critical"][worst]]}`), el.home.a.firstChild);
    el.home.counts.replaceChildren(...countsLine(sum));
  }
  for (const t of TABS) {
    const { a, counts } = el.tabs[t];
    a.setAttribute("aria-current", route.parts[0] === t || (route.parts[0] === "c" && store.comps[route.parts[1]]?.tab === t) ? "page" : "false");
    const n = node(t);
    a.replaceChild(dot(n.s, `${TAB_TITLE[t]}: ${STATUS_LABEL[["ok", "unknown", "offline", "warning", "critical"][n.s]]}`), a.firstChild);
    counts.replaceChildren(...countsLine(Object.fromEntries(Object.entries(n.counts || {}).filter(([k]) => k !== "ok"))));
  }
}

function buildBanners() {
  const box = document.getElementById("banners");
  clear(box);
  for (const n of store.meta.notes || []) box.append(h("div", { class: "banner" }, h("b", { text: "Note:" }), n));
  el.bf = h("div", { class: "banner info hidden" }, "Building simulated history…", h("div", { class: "progress" }, h("span", {})), h("span", { class: "num" }));
  el.upd = h("div", { id: "update-banners" });
  box.append(el.bf, el.upd);
}

function updateBackfill() {
  const pct = store.backfill;
  el.bf.classList.toggle("hidden", pct >= 100);
  el.bf.querySelector(".progress span").style.width = `${pct}%`;
  setText(el.bf.lastChild, `${Math.round(pct)}%`);
}

function buildFooter() {
  const f = document.getElementById("footer");
  f.append(
    h("button", { class: "linkish", type: "button", text: `Version ${store.meta.version} · What's new`, onclick: () => openWhatsNew(false) }),
    h("span", { text: `Times are ${tz()} (site time)` }),
    h("span", {}, store.meta.mode === "sim" ? "Data: simulator" : "Data: live devices",
      (store.meta.info || []).length ? [" · ", h("button", { class: "linkish", type: "button", text: "What's checked",
        onclick: openChecked })] : null),
    h("span", { text: `History store: ${store.meta.tsdb === "memory" ? "in memory" : "VictoriaMetrics"}` }),
    el.updFoot = h("span", { text: "Updates: …" }));
}

// ---------------------------------------------------------------------------
// events drawer
let drawer = null;
async function openEvents() {
  if (!drawer) {
    const list = h("ul");
    const onlyBad = h("input", { type: "checkbox", checked: true });
    const comps = h("input", { type: "checkbox", checked: true });
    const hidden = h("input", { type: "checkbox" });
    const info = h("input", { type: "checkbox", checked: true });
    const system = h("input", { type: "checkbox", checked: true });
    const title = h("h2");
    const scrim = h("div", { class: "scrim hidden" });
    const panel = h("aside", { class: "drawer", "aria-label": "Events" },
      h("header", {}, title, h("button", { class: "xbtn", type: "button", "aria-label": "Close", text: "×", onclick: () => close() })),
      h("div", { class: "filters" },
        h("label", {}, onlyBad, "Warnings and worse"),
        h("label", {}, comps, "Single devices"),
        h("label", {}, hidden, "Devices hidden behind a failed one"),
        h("label", {}, info, "Address & firmware changes"),
        h("label", {}, system, "Updates, reports & sign-ins")),
      list);
    const close = () => { panel.classList.remove("open"); scrim.classList.add("hidden"); };
    scrim.addEventListener("click", close);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    document.body.append(scrim, panel);
    const load = async () => {
      const w = window_();
      const from = w.live ? Date.now() / 1000 - 86400 : w.from;
      setText(title, w.live ? "Events · last 24 hours" : "Events · selected range");
      const rows = await get("/api/events", { from, to: w.to, min_status: onlyBad.checked ? 3 : 0, suppressed: hidden.checked ? "true" : "",
        kinds: comps.checked ? "component,group" : "group", info: info.checked ? "true" : "", system: system.checked ? "true" : "", limit: 400 });
      clear(list);
      if (!rows.length) list.append(h("li", { class: "muted", style: "padding:14px 16px", text: "No events." }));
      for (const e of rows) list.append(h("li", {}, eventLink(e, (href) => { close(); nav(href); })));
    };
    for (const c of [onlyBad, comps, hidden, info, system]) c.addEventListener("change", load);
    drawer = { panel, scrim, load };
  }
  drawer.scrim.classList.remove("hidden");
  drawer.panel.classList.add("open");
  drawer.load();
}

async function updateEventCount() {
  try {
    const rows = await get("/api/events", { from: Date.now() / 1000 - 3600, min_status: 3, kinds: "group", limit: 99 });
    const n = rows.filter((r) => r.new >= 3).length;
    el.evCount.classList.toggle("hidden", !n);
    setText(el.evCount, String(n));
    el.evBtn.title = n ? `${n} new problems in the last hour` : "No new problems in the last hour";
  } catch { /* server restarting */ }
}

// ---------------------------------------------------------------------------
// modals
async function openWhatsNew(auto) {
  const changes = await get("/api/changelog");
  const prev = lsGet("ng_last_version");
  const idx = changes.findIndex((c) => c.version === prev);
  const newer = auto && idx > 0 ? changes.slice(0, idx) : changes.slice(0, 1);
  const older = changes.filter((c) => !newer.includes(c));
  const section = (c, cls) => [h("h3", { class: cls, text: `Version ${c.version} (${c.date})` }), h("ul", { class: cls }, c.items.map((i) => h("li", { text: i })))];
  modal(auto ? `What's new since version ${prev}` : "What's new", [newer.map((c) => section(c)), older.map((c) => section(c, "older"))]);
  lsSet("ng_last_version", store.meta.version);
}

// information about this install (0.9.4): what is checked and read - not a to-do, so not a banner
function openChecked() {
  modal("What's checked", [
    h("ul", {}, (store.meta.info || []).map((n) => h("li", { text: n }))),
    h("p", { class: "muted", text: "Things that need doing are the yellow notes at the top of the page." }),
  ]);
}

function openHelp() {
  const st = (s, txt) => h("tr", {}, h("td", {}, badge(s, { small: true })), h("td", { text: txt }));
  modal("How to read this dashboard", [
    h("p", { text: "The Overview sums up the site: a panel each for Vehicles, Control Room and Infrastructure, and everything that needs attention now. The three tabs have a card per vehicle, control-room station or site location. Click a card for its detail page; click a device in a table for everything about that device. The Map shows every location and vehicle where its GPS last put it." }),
    h("h3", { text: "Statuses" }),
    h("table", { class: "legend-table" }, h("tbody", {},
      st(0, "Every reading is inside its limits."),
      st(3, "A reading is past its warning limit (for example ping above 250 ms, battery below 40 %)."),
      st(4, "A reading is past its critical limit, or a device that should always be up is not responding."),
      st(2, "Not responding where that is normal - a parked vehicle or a desk nobody is logged in to. Not an alarm."),
      st(1, "No data yet, data stopped coming, or the device is behind one that has failed ('unreachable via …').")) ),
    h("h3", { text: "Live and History" }),
    h("p", { text: "Live updates every second and shows the last hour in charts. History shows the chosen range (1 h to 30 days, or a custom range): cards then show availability and a status strip, and the vehicle path shows availability and 95th-percentile latency for each link." }),
    h("p", { text: "Availability counts time OK or Warning against time with a known status. Offline time of a parked vehicle is not counted against it; time when the dashboard itself was not running counts as unknown." }),
    h("h3", { text: "Where the numbers come from" }),
    h("ul", {}, h("li", { text: "VCU, GNSS and VAI: from the Mobius Server's own database (the same data Mobius uses)." }),
      h("li", { text: "Radios, routers, Starlink, Tachyon, Ubiquiti, Celona: read from each device by the dashboard server." }),
      h("li", { text: "Battery and solar: from the Victron Cerbo GX on each trailer." })),
    h("h3", { text: "Notes" }),
    h("p", { text: "A yellow note at the top is something to do (for example a device login to add). What the dashboard checks and reads is under \"What's checked\" at the bottom of the page." }),
    h("h3", { text: "Feedback" }),
    h("p", { text: "Feedback (top right) sends a request, a problem or an idea to the dashboard's admins - no sign-in needed. They track it on the Admin page, and new ones are in the morning report. Report a problem is different: an admin downloads a zip for whoever looks into a fault of the dashboard itself." }),
    h("p", { class: "muted", text: `Times are shown in the site's time zone (${tz()}). The limits are in rules_default.yaml on the server and are starting values for ops to review.` }),
  ]);
}

// ---------------------------------------------------------------------------
watchErrors();                        // before anything else can fail

async function main() {
  const [meta, inv, live] = await Promise.all([get("/api/meta"), get("/api/inventory"), get("/api/live")]);
  store.meta = meta;
  store.nodes = live.nodes;           // first paint already has statuses; the WebSocket takes over
  store.backfill = meta.backfill.pct;
  setup(meta);
  indexInventory(inv);
  buildHeader();
  buildBanners();
  buildTabs();
  buildFooter();
  buildWallBar();
  initUpdates(el.upd, el.updFoot);
  let building = store.backfill < 100;
  subscribe((kind) => {
    if (kind === "live") {
      if (building && store.backfill >= 100) {      // history finished building: redraw with it
        building = false;
        render(true);
      }
      current?.update?.();
      updateTabs();
      updateErrors();
      updateBackfill();
    }
    tickConn();
  });
  window.addEventListener("hashchange", () => render());
  connectLive();
  render();
  startToasts({ nav, openErrors });
  updateErrors();
  startMobiusSummary();
  setInterval(tickConn, 1000);
  updateEventCount();
  setInterval(updateEventCount, 60000);
  const prev = lsGet("ng_last_version");
  if (prev && prev !== meta.version) openWhatsNew(true);
  else if (!prev) lsSet("ng_last_version", meta.version);
  notify("live");
}

main().catch((e) => {
  const m = document.getElementById("main");
  clear(m);
  m.append(h("p", { text: `Could not load the dashboard: ${e.message}` }));
});
