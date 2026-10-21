// Admin page: sign-in, updates (check, review, offline upload, progress), morning report
// settings and test send, which secrets are set (never their values), recent system events.

import { errorText, get, post } from "../api.js";
import { clear, h, setText } from "../dom.js";
import { fmtWhen, tz } from "../fmt.js";
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
    body.append(updatesPanel(status.updates), reportPanel(status), mapPanel(status), secretsPanel(status.secrets), eventsPanel(nav));
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
