// Admin page: sign-in, updates (check, review, offline upload, progress), morning report
// settings and test send, which secrets are set (never their values), recent system events.

import { errorText, get, post } from "../api.js";
import { append, clear, h, setText } from "../dom.js";
import { fmtWhen, tz } from "../fmt.js";
import { modal } from "../modal.js";
import { indexInventory, store } from "../store.js";
import { admin, GIT_UPDATE, onUpdates, openReview, refreshSession, setUpdates, signInForm } from "../updates.js";
import { eventLink } from "./common.js";

const JOB_TEXT = { idle: "Nothing in progress", downloading: "Downloading", verifying: "Checking", requested: "Handed to the server's updater",
  installing: "Installing", staged: "Downloaded and checked", failed: "Not installed" };

export function adminView(root, nav) {
  const head = h("div", { class: "page-head" }, h("h1", { text: "Admin" }), h("span", { class: "sub" }));
  const body = h("div", { class: "admin" });
  root.append(head, body);
  let status = null;
  let alive = true;

  async function load() {
    await refreshSession().catch(() => {});
    if (!alive) return;
    clear(body);
    setText(head.querySelector(".sub"), admin.signedIn ? `Signed in as ${admin.name}` : "Only needed to approve updates and change the morning report");
    if (!admin.signedIn) {
      body.append(h("section", { class: "panel pad narrow" }, h("h2", { text: "Sign in" }), signInForm(load)));
      return;
    }
    try {
      status = await get("/api/admin/status");
    } catch (e) {
      body.append(h("p", { text: `Could not load: ${e.message}` }));
      return;
    }
    head.append(h("button", { class: "btn", type: "button", text: "Sign out", onclick: async () => {
      await post("/api/admin/logout");
      await refreshSession();
      head.lastChild.remove();
      load();
    } }));
    body.append(updatesPanel(status.updates), reportPanel(status), mapPanel(status), livePanel(status),
      discoveryPanel(nav, () => alive), secretsPanel(status.secrets), eventsPanel(nav));
  }

  const off = onUpdates(() => { if (status && admin.signedIn) refreshUpdatesPanel(); });
  async function refreshUpdatesPanel() {
    try {
      const st = await get("/api/admin/status");
      if (!alive) return;
      status = st;
      const old = body.querySelector("#upd-panel");
      if (old) old.replaceWith(updatesPanel(st.updates));
    } catch { /* restarting */ }
  }

  load();
  return { destroy() { alive = false; off(); } };
}

// ---------------------------------------------------------------------------
function row(k, ...v) {
  return h("tr", {}, h("td", { text: k }), h("td", {}, ...v));
}

function updatesPanel(u) {
  const msg = h("div", { class: "form-msg", role: "alert" });
  const job = u.job || { state: "idle" };
  const check = u.check || {};
  const latest = u.latest;
  const checkBtn = h("button", { class: "btn", type: "button", text: "Check now", onclick: async () => {
    checkBtn.disabled = true;
    setText(msg, "Asking GitHub…");
    try { setUpdates(await post("/api/admin/update/check")); setText(msg, ""); } catch (e) { setText(msg, e.message); }
    checkBtn.disabled = false;
  } });
  const git = u.deploy === "compose";
  const how = git ? " · test install from the GitHub image (update: docker compose pull, then docker compose up -d)"
    : u.can_install ? " · installed with install.sh (updates install here)" : " · this PC (updates download and are checked, not installed)";
  const table = h("table", { class: "info-table" }, h("tbody", {},
    row("Running", h("b", { text: u.current }), how),
    row("Last check", check.at ? `${fmtWhen(check.at)} - ` : "never - ", h("span", { class: check.ok === false ? "warn-text" : "", text: check.message || "" })),
    row("Next check", `${fmtWhen(u.next_check)} (daily at the time set below, ${tz()})`),
    row("Newest release", latest ? `${latest.version}${latest.published_at ? " · " + fmtWhen(Date.parse(latest.published_at) / 1000) : ""}${latest.newer ? "" : " (not newer)"}` : "–",
      u.offered ? h("button", { class: "btn primary", type: "button", text: "Review update", style: "margin-left:10px", onclick: openReview }) : null),
    row("In progress", h("b", { text: JOB_TEXT[job.state] || job.state }), job.version ? ` · ${job.version}` : "", job.message ? h("div", { class: "muted", text: job.message }) : null),
    u.skipped?.length ? row("Skipped", u.skipped.join(", ")) : null,
    u.result ? row("Last install", u.result.text || "", u.result.at ? h("span", { class: "muted", text: ` (${fmtWhen(u.result.at)})` }) : null,
      (u.result.warnings || []).map((w) => h("div", { class: "muted", text: w }))) : null,
    row("GitHub token", u.token_set ? "set (secrets.env)" : "not set - not needed (the repository is public)")));
  return h("section", { class: "panel pad", id: "upd-panel" }, h("h2", { text: "Updates" }), table,
    h("div", { class: "actions-row" }, checkBtn), msg,
    git ? h("p", { class: "muted", text: `To update this install: ${GIT_UPDATE}.` }) : uploadBox(u));
}

function uploadBox(u) {
  const zip = h("input", { type: "file", accept: ".zip", "aria-label": "Update zip" });
  const sha = h("input", { type: "file", accept: ".sha256", "aria-label": "Checksum file" });
  const bar = h("div", { class: "progress hidden" }, h("span", {}));
  const msg = h("div", { class: "form-msg", role: "alert" });
  const btn = h("button", { class: "btn", type: "button", text: "Upload and install", onclick: async () => {
    setText(msg, "");
    const z = zip.files[0], s = sha.files[0];
    if (!z || !s) return setText(msg, "Choose both files: NetworkGuru_v<version>.zip and its .sha256.");
    const m = /^NetworkGuru_v(\d+\.\d+\.\d+)\.zip$/.exec(z.name);
    if (!m) return setText(msg, "The zip must be named NetworkGuru_v<version>.zip, as published.");
    const shaText = (await s.text()).trim().split(/\r?\n/)[0];
    const what = u.can_install ? "The server rebuilds and restarts the dashboard (about a minute)." : "This PC only checks it.";
    if (!confirm(`Install Network Guru ${m[1]} from ${z.name}?\n${what}`)) return;
    btn.disabled = true;
    bar.classList.remove("hidden");
    try {
      const st = await sendZip(z, shaText, (p) => { bar.firstChild.style.width = `${p}%`; });
      setUpdates(st);
      setText(msg, `Version ${st.version} uploaded and checked.`);
    } catch (e) { setText(msg, e.message); }
    btn.disabled = false;
    bar.classList.add("hidden");
  } });
  return h("div", { class: "upload" }, h("h3", { text: "No internet at this site? Upload the update" }),
    h("p", { class: "muted", text: "Both files are on the GitHub release page. The same checks run as for a download: checksum, the version inside, newer than the running one." }),
    h("div", { class: "actions-row" }, h("label", {}, "Zip ", zip), h("label", {}, ".sha256 ", sha), btn), bar, msg);
}

// XHR (not fetch) so a large zip shows its progress.
function sendZip(file, shaText, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", `/api/admin/update/upload?name=${encodeURIComponent(file.name)}`);
    x.setRequestHeader("Content-Type", "application/zip");
    x.setRequestHeader("X-Requested-With", "networkguru");
    x.setRequestHeader("X-Update-Sha256", shaText);
    x.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(Math.round((100 * e.loaded) / e.total)); };
    x.onload = async () => {
      if (x.status >= 200 && x.status < 300) resolve(JSON.parse(x.responseText));
      else reject(new Error(await errorText(new Response(x.responseText, { status: x.status }))));
    };
    x.onerror = () => reject(new Error("The upload was interrupted."));
    x.send(file);
  });
}

// ---------------------------------------------------------------------------
function reportPanel(status) {
  const s = status.settings;
  const rep = status.report;
  const f = {};
  const input = (key, attrs = {}) => (f[key] = h("input", { type: "text", value: s[key] ?? "", ...attrs }));
  const check = (key) => (f[key] = h("input", { type: "checkbox", checked: !!s[key] }));
  const select = (key, opts) => (f[key] = h("select", { class: "btn" }, ...opts.map(([v, t]) => h("option", { value: v, text: t, selected: s[key] === v }))));
  const field = (label, el, hint) => h("label", { class: "field" }, h("span", { text: label }), el, hint ? h("small", { class: "muted", text: hint }) : null);
  f.email_to = h("textarea", { rows: 2, text: (s.email_to || []).join(", ") });
  const graphBox = h("div", { class: "fields" },
    field("Tenant ID", input("graph_tenant", { placeholder: "00000000-0000-0000-0000-000000000000" })),
    field("App (client) ID", input("graph_client_id")),
    field("Send as (mailbox)", input("graph_sender", { placeholder: "networkguru@asirobots.com" }), "The app's client secret is in secrets.env"));
  const smtpBox = h("div", { class: "fields" },
    field("SMTP server", input("smtp_host")), field("Port", input("smtp_port", { inputmode: "numeric", size: 6 })),
    field("Security", select("smtp_security", [["starttls", "STARTTLS"], ["ssl", "SSL/TLS"], ["none", "None (relay, no sign-in)"]])),
    field("From", input("email_from")));
  const via = select("email_via", [["graph", "Microsoft 365 (Graph)"], ["smtp", "SMTP"]]);
  const showVia = () => { graphBox.classList.toggle("hidden", via.value !== "graph"); smtpBox.classList.toggle("hidden", via.value !== "smtp"); };
  via.addEventListener("change", showVia);
  showVia();
  const msg = h("div", { class: "form-msg", role: "alert" });
  const save = h("button", { class: "btn primary", type: "button", text: "Save", onclick: async () => {
    const patch = {};
    for (const [k, el] of Object.entries(f)) patch[k] = el.type === "checkbox" ? el.checked : el.value;
    save.disabled = true;
    try { await post("/api/admin/settings", patch, "PUT"); setText(msg, "Saved."); } catch (e) { setText(msg, e.message); }
    save.disabled = false;
  } });
  const testMsg = h("div", { class: "form-msg" });
  const test = h("button", { class: "btn", type: "button", text: "Send a test report now", onclick: async () => {
    test.disabled = true;
    setText(testMsg, "Sending…");
    try {
      const r = await post("/api/admin/report/test");
      setText(testMsg, Object.entries(r).map(([k, v]) => `${k}: ${v}`).join(" · "));
    } catch (e) { setText(testMsg, e.message); }
    test.disabled = false;
  } });
  const chans = Object.entries(rep.channels || {});
  const setup = Object.entries(rep.setup || {}).filter(([, why]) => why);
  return h("section", { class: "panel pad" }, h("h2", { text: "Morning report" }),
    h("p", { class: "muted", text: "Sent every day, even when everything is green - a missing report means this server needs a look. Red and yellow first, then what changed since yesterday." }),
    h("table", { class: "info-table" }, h("tbody", {},
      row("Last report", rep.date ? `${rep.date}${rep.done ? "" : " (in progress)"}${rep.late ? " (late)" : ""}` : "none yet",
        chans.map(([k, v]) => h("div", { text: `${k}: ${v.ok ? "✓" : "✗"} ${v.message || ""}${v.ok ? "" : ` (try ${v.tries} of 3)`}` })),
        rep.note ? h("div", { class: "warn-text", text: rep.note }) : null),
      setup.length ? row("Not set up", ...setup.map(([k, why]) => h("div", { class: "warn-text", text: `${k}: ${why}` }))) : null)),
    h("div", { class: "fields" },
      field("Send at (site time)", input("report_time", { size: 6, placeholder: "06:30" })),
      field("Check for updates at", input("update_check_time", { size: 6, placeholder: "06:00" })),
      field("Dashboard address (link in the report)", input("dashboard_url"), "As people type it in the browser, with :8080")),
    h("div", { class: "fields" }, h("label", { class: "check" }, check("report_teams"), "Post to Teams"),
      h("label", { class: "check" }, check("report_email"), "Send e-mail")),
    h("div", { class: "fields" }, field("E-mail to (comma separated)", f.email_to), field("Send e-mail with", via)),
    graphBox, smtpBox,
    h("div", { class: "actions-row" }, save, h("a", { href: "#/report", text: "Preview the report" }), test), msg, testMsg);
}

const MAP_CHOICES = [
  ["satellite", "Satellite (Esri World Imagery)"],
  ["street", "Street map (OpenStreetMap)"],
  ["none", "None - plain grid, never contacts a map service"],
];

function mapPanel(st) {
  const msg = h("div", { class: "form-msg", role: "status" });
  const current = st.settings.map_background || "satellite";
  const pick = h("select", { "aria-label": "Map background" },
    ...MAP_CHOICES.map(([v, label]) => h("option", { value: v, text: label, selected: v === current })));
  const save = h("button", { class: "btn primary", type: "button", text: "Save", onclick: async () => {
    save.disabled = true;
    setText(msg, "");
    try { await post("/api/admin/settings", { map_background: pick.value }, "PUT"); setText(msg, "Saved - reopen the Map page to see it."); }
    catch (e) { setText(msg, e.message); }
    save.disabled = false;
  } });
  return h("section", { class: "panel pad" }, h("h2", { text: "Map" }),
    h("p", { class: "muted", text: "The Map page's background. The viewer's browser loads the map images straight from the provider "
      + "(the server never does). Without internet it falls back to a plain grid - the pins are placed by GPS either way. "
      + "Check that ASI's use fits the provider's terms (Esri may need an ArcGIS licence)." }),
    h("div", { class: "fields" }, h("label", { class: "field" }, h("span", { text: "Background" }), pick)),
    h("div", { class: "actions-row" }, save, h("a", { href: "#/map", text: "Open the map" })), msg);
}

// Live view: groups that have gone quiet drop off Live and the map (quiet.js)
function livePanel(st) {
  const msg = h("div", { class: "form-msg", role: "status" });
  const mins = h("input", { type: "number", min: "0", max: "10080", step: "5", size: 6, "aria-label": "Minutes",
    value: String(st.settings.hide_quiet_min ?? 60) });
  const on = new Set(st.settings.hide_quiet_tabs || []);
  const boxes = [["vehicles", "Vehicles"], ["control", "Control Room"], ["infrastructure", "Infrastructure"]].map(([v, label]) => {
    const box = h("input", { type: "checkbox", value: v, checked: on.has(v) });
    return { v, box, el: h("label", { class: "check" }, box, ` ${label}`) };
  });
  const save = h("button", { class: "btn primary", type: "button", text: "Save", onclick: async () => {
    save.disabled = true;
    setText(msg, "");
    const patch = { hide_quiet_min: Number(mins.value || 0), hide_quiet_tabs: boxes.filter((b) => b.box.checked).map((b) => b.v) };
    try {
      await post("/api/admin/settings", patch, "PUT");
      store.meta.hide_quiet = { min: patch.hide_quiet_min, tabs: patch.hide_quiet_tabs };
      setText(msg, "Saved.");
    } catch (e) { setText(msg, e.message); }
    save.disabled = false;
  } });
  return h("section", { class: "panel pad" }, h("h2", { text: "Live view" }),
    h("p", { class: "muted", text: "A vehicle, station or location none of whose devices has answered for this long drops off the "
      + "Live view and the map (a switch on each page shows it again). History, its own page, alerts and the morning "
      + "report keep it. Untick Infrastructure if a dead trailer must never drop out of sight." }),
    h("div", { class: "fields" }, h("label", { class: "field" }, h("span", { text: "Hide after (minutes, 0 = never)" }), mins),
      h("div", { class: "field" }, h("span", { text: "On the tabs" }), h("div", { class: "checks" }, boxes.map((b) => b.el)))),
    h("div", { class: "actions-row" }, save), msg);
}

// The discovery scan (found.py): what answers on the site's networks, for review. Moved devices are
// followed by the server itself; new ones wait here for Add (or Ignore).
const TAB_NAME = { vehicles: "Vehicles", control: "Control Room", infrastructure: "Infrastructure" };

function discoveryPanel(nav, isAlive) {
  const el = h("section", { class: "panel pad", id: "disc-panel" }, h("h2", { text: "Found on the network" }),
    h("p", { class: "muted", text: "The network scan looks for the site's devices around the addresses the inventory has "
      + "(and the Sites sheet's Subnets): Mobius servers, InfluxDB and NTRIP casters by their ports, radios, routers, "
      + "modems and switches by SNMP with the site's own logins. A device that moved is followed by itself; a new one "
      + "waits here. Tell IT before the first scan - network security tools may notice it." }));
  const box = h("div", {});
  el.append(box);
  let timer = null;

  async function load(flash = null) {
    let d;
    try { d = await get("/api/admin/discovery"); } catch (e) { box.replaceChildren(h("p", { text: e.message })); return; }
    if (!isAlive()) return;
    draw(d);
    if (flash) {
      const p = h("p", { class: "flash", role: "status" }, flash.text);
      if (flash.href) {
        const open = h("a", { href: flash.href, text: "Open it" });
        open.addEventListener("click", (e) => { e.preventDefault(); nav(flash.href); });
        p.append(" ", open);
      }
      box.prepend(p);
    }
    clearTimeout(timer);
    if (d.running) timer = setTimeout(load, 2000);
  }

  function draw(d) {
    clear(box);
    if (!d.enabled) { box.append(h("p", { class: "muted", text: d.why || "Not available." })); return; }
    const msg = h("div", { class: "form-msg", role: "status" });
    const last = d.last;
    const summary = !last ? "No scan yet."
      : last.error ? `Last scan ${fmtWhen(last.ts)}: ${last.error}`
        : `Last scan ${fmtWhen(last.ts)}${last.who && last.who !== "nightly" ? ` by ${last.who}` : " (nightly)"}: `
          + `${last.addresses} addresses in ${last.ranges.length} range${last.ranges.length === 1 ? "" : "s"}, ${last.answered} answered, `
          + `${d.known} already in the inventory · ${Math.max(1, Math.round(last.duration_s / 60))} min`;
    const scan = h("button", { class: "btn primary", type: "button", text: d.running ? "Scanning…" : "Scan now", disabled: d.running,
      onclick: async () => {
        scan.disabled = true;
        try { await post("/api/admin/discovery/scan"); load(); } catch (e) { setText(msg, e.message); scan.disabled = false; }
      } });
    const when = h("input", { type: "text", size: 6, value: d.time || "", placeholder: "02:00", "aria-label": "Nightly scan time" });
    const nets = h("input", { type: "text", class: "disc-nets", value: (d.ranges || []).join(", "),
      placeholder: "e.g. 10.200.1.0/24, 10.200.4.0/24", "aria-label": "Also scan these networks" });
    const saveWhen = h("button", { class: "btn", type: "button", text: "Save", onclick: async () => {
      saveWhen.disabled = true;
      try {
        await post("/api/admin/settings", { discovery_time: when.value.trim(), discovery_ranges: nets.value }, "PUT");
        await load({ text: "Saved - the next scan uses them.", href: null });
      } catch (e) { setText(msg, e.message); saveWhen.disabled = false; }
    } });
    const csv = h("a", { class: "btn", href: "/api/admin/discovery/export.csv", text: "Download new ones for devices.xlsx" });
    append(box, [h("p", { text: summary }),
      last && last.ranges?.length ? h("p", { class: "muted small", text: `Ranges: ${last.ranges.join(", ")}` }) : null,
      d.running && d.progress ? h("p", { class: "ink2", text: `Scanning - ${d.progress.phase}: ${d.progress.done} of ${d.progress.total}` }) : null,
      h("div", { class: "actions-row" }, scan, csv),
      h("div", { class: "actions-row" },
        h("label", { class: "field disc-nets-field" }, h("span", { text: "Also scan these networks" }), nets,
          h("small", { class: "muted", text: "Private ranges only. The networks around the inventory's devices and the "
            + "Sites sheet's Subnets are always scanned." })),
        h("label", { class: "field" }, h("span", { text: "Nightly at (site time, empty = off)" }), when), saveWhen),
      msg]);
    const list = (title, rows, cols, render, open = true) => {
      if (!rows.length) return;
      const table = h("table", { class: "comps disc" }, h("thead", {}, h("tr", {}, ...cols.map((t) => h("th", { text: t })))),
        h("tbody", {}, rows.map(render)));
      const wrap = h("div", { class: "table-scroll" }, table);
      box.append(open ? h("div", {}, h("h3", { class: "mob-h3", text: `${title} (${rows.length})` }), wrap)
        : h("details", { class: "mob-site" }, h("summary", { text: `${title} (${rows.length})` }), wrap));
    };
    const seen = (x) => h("td", { class: "muted", text: x.first && x.last && x.first !== x.last ? `${fmtWhen(x.first)} - ${fmtWhen(x.last)}` : fmtWhen(x.last) });
    const what = (x) => h("td", {}, h("b", { text: x.label }), x.model ? h("div", { class: "muted small", text: x.model }) : null);
    const items = d.items || [];
    const fresh = items.filter((x) => !x.ignored && x.outcome === "new");
    const changed = items.filter((x) => !x.ignored && x.outcome === "changed");
    list("New", fresh, ["What", "Name", "Address", "How it was found", "Seen", ""], (x) => h("tr", {}, what(x),
      h("td", { text: x.name || "–" }), h("td", { class: "num", text: x.address }), h("td", { class: "muted", text: x.how.join("; ") }),
      seen(x), h("td", {}, h("button", { class: "btn", type: "button", text: "Add…", onclick: () => addDialog(x, d, nav, load) }),
        " ", h("button", { class: "linkish", type: "button", text: "Ignore", onclick: () => ignore(x.key, true) }))));
    list("Answers as something else", changed, ["What it says it is", "Address", "The inventory has", "How", ""], (x) => h("tr", {}, what(x),
      h("td", { class: "num", text: x.address }), h("td", { text: x.comp_name || "–" }), h("td", { class: "muted", text: x.how.join("; ") }),
      h("td", {}, h("button", { class: "linkish", type: "button", text: "Ignore", onclick: () => ignore(x.key, true) }))));
    list("Moved - followed to their new address", d.moves || [], ["Device", "From", "To", "When", "Known by"], (m) => h("tr", {},
      h("td", { text: m.name }), h("td", { class: "num", text: m.old || "–" }), h("td", { class: "num", text: m.new }),
      h("td", { class: "muted", text: fmtWhen(m.ts) }), h("td", { class: "muted", text: m.how })));
    list("Added from the scan", items.filter((x) => x.outcome === "added"), ["What", "Address", "Now in the dashboard as"], (x) => h("tr", {},
      what(x), h("td", { class: "num", text: x.address }), h("td", { text: x.comp_name || x.comp || "–" })), false);
    list("Ignored", items.filter((x) => x.ignored), ["What", "Name", "Address", ""], (x) => h("tr", {}, what(x),
      h("td", { text: x.name || "–" }), h("td", { class: "num", text: x.address }),
      h("td", {}, h("button", { class: "linkish", type: "button", text: "Show again", onclick: () => ignore(x.key, false) }))), false);
    list("Other hosts that answered (not recognised)", d.others || [], ["Address", "Name", "MAC"], (o) => h("tr", {},
      h("td", { class: "num", text: o.address }), h("td", { text: o.name || "–" }), h("td", { class: "num", text: o.mac || "–" })), false);
    if (last && !last.error && !fresh.length && !changed.length) box.append(h("p", { class: "muted", text: "Nothing new to review." }));
  }

  async function ignore(key, on) {
    try { await post("/api/admin/discovery/ignore", { key, ignore: on }); } catch { /* shown on the next load */ }
    load();
  }

  load();
  return el;
}

function addDialog(x, d, nav, done) {
  const roleKind = { mobius_server: "server", influxdb: "server", mobius_client: "client", gnss_base: "tower" };
  const mobiusGroup = d.groups.find((g) => (x.type === "mobius_server" || x.type === "influxdb") && g.kind === "server");
  const name = h("input", { type: "text", value: x.name || `${x.label} ${x.address}`, maxlength: 60 });
  const type = h("select", { class: "btn" }, ...Object.entries(d.types).sort((a, b) => a[1].localeCompare(b[1]))
    .map(([k, label]) => h("option", { value: k, text: label, selected: k === x.type })));
  const group = h("select", { class: "btn" }, h("option", { value: "", text: "A new group…" }),
    ...["vehicles", "control", "infrastructure"].map((tab) => h("optgroup", { label: TAB_NAME[tab] },
      ...d.groups.filter((g) => g.tab === tab).map((g) => h("option", { value: g.id, text: g.name, selected: g.id === mobiusGroup?.id })))));
  const gname = h("input", { type: "text", value: x.name || "", maxlength: 60, placeholder: "e.g. Truck 14 or Mobius PC 2" });
  const kind = h("select", { class: "btn" }, ...d.kinds.map((k) => h("option", { value: k, text: k, selected: k === (roleKind[x.type] || "tower") })));
  const newBox = h("div", { class: "fields" }, h("label", { class: "field" }, h("span", { text: "New group name" }), gname),
    h("label", { class: "field" }, h("span", { text: "Kind" }), kind));
  const showNew = () => newBox.classList.toggle("hidden", group.value !== "");
  group.addEventListener("change", showNew);
  showNew();
  const msg = h("div", { class: "form-msg", role: "alert" });
  let close = null;
  const ok = h("button", { class: "btn primary", type: "button", text: "Add to the dashboard", onclick: async () => {
    ok.disabled = true;
    setText(msg, "");
    try {
      const r = await post("/api/admin/discovery/add", { key: x.key, name: name.value, type: type.value, group_id: group.value || null,
        group_name: gname.value, kind: kind.value });
      indexInventory(await get("/api/inventory"));          // this page knows the new device at once
      close();
      done({ text: `Added: ${name.value} - checked from now on, in ${TAB_NAME[r.tab]} → ${store.groups[r.group]?.name || r.group}.`,
        href: `#/${r.tab}/${r.group}` });
    } catch (e) { setText(msg, e.message); ok.disabled = false; }
  } });
  const body = h("div", {},
    h("p", { class: "muted", text: `${x.label} at ${x.address}${x.how.length ? ` - ${x.how.join("; ")}` : ""}. It is checked from now on `
      + "(no restart); the site's devices.xlsx is not changed - use Download for devices.xlsx to bring it there too." }),
    h("div", { class: "fields" }, h("label", { class: "field" }, h("span", { text: "Name" }), name),
      h("label", { class: "field" }, h("span", { text: "Type" }), type),
      h("label", { class: "field" }, h("span", { text: "Group" }), group)),
    newBox, h("div", { class: "actions-row" }, ok), msg);
  close = modal("Add a found device", body);
}

function secretsPanel(list) {
  return h("section", { class: "panel pad" }, h("h2", { text: "Secrets on this server" }),
    h("p", { class: "muted", text: admin.deploy === "compose"
      ? "Values are never shown or sent to the browser. Change them in dev-secrets/secrets.env next to compose.yaml (read again within seconds)."
      : "Values are never shown or sent to the browser. Change them on the server: sudo bash deploy/install.sh --secrets" }),
    h("table", { class: "info-table" }, h("tbody", {}, list.map((x) => row(x.label, h("span", { class: x.set ? "" : "muted", text: x.set ? "✓ set" : "– not set" }),
      h("span", { class: "muted", text: `  ${x.name}` }))))));
}

function eventsPanel(nav) {
  const list = h("ul", { class: "plain" });
  const el = h("section", { class: "panel pad" }, h("h2", { text: "Updates, reports and sign-ins (7 days)" }), list);
  get("/api/events", { from: Date.now() / 1000 - 7 * 86400, kinds: "system", system: "true", limit: 60 }).then((rows) => {
    if (!rows.length) list.append(h("li", { class: "muted", text: "Nothing yet." }));
    for (const e of rows) list.append(h("li", {}, eventLink(e, nav)));
  }).catch(() => {});
  return el;
}
