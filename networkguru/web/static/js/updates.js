// Update banner (everyone sees it), the Review dialog (only a signed-in admin can act) and the
// admin sign-in state shared with the Admin page.

import { get, post } from "./api.js";
import { clear, h, setText } from "./dom.js";
import { fmtWhen } from "./fmt.js";
import { modal } from "./modal.js";
import { store } from "./store.js";

export const admin = { configured: false, signedIn: false, name: null, devPassword: false, deploy: "dev" };
export let updates = null;              // last /api/update/status answer
// the developer install from git (compose.yaml at the top of the repository) updates itself this way
export const GIT_UPDATE = "git pull, then docker compose up -d --build, in the Network-Guru folder";

let box = null;
let footer = null;
let timer = null;
const listeners = new Set();

export function onUpdates(fn) { listeners.add(fn); return () => listeners.delete(fn); }

export async function refreshSession() {
  const s = await get("/api/admin/session");
  Object.assign(admin, { configured: s.configured, signedIn: s.signed_in, name: s.name, devPassword: s.dev_password, deploy: s.deploy || "dev" });
  if (updates) render();                // the Dismiss buttons depend on being signed in
  for (const fn of [...listeners]) fn();
  return admin;
}

export function initUpdates(bannerBox, footerEl) {
  box = bannerBox;
  footer = footerEl;
  refreshSession().catch(() => {});
  refreshUpdates();
}

// While something is happening, look more often: a download takes seconds, an install a minute.
function pollMs() {
  const st = updates?.job?.state;
  if (!updates) return 15000;
  if (["downloading", "verifying"].includes(st)) return 2000;
  if (["requested", "installing"].includes(st)) return 10000;
  return 300000;
}

export async function refreshUpdates() {
  clearTimeout(timer);
  try {
    updates = await get("/api/update/status");
    if (store.meta && updates.current !== store.meta.version) {      // the server was updated: load the new pages
      location.reload();
      return;
    }
    render();
    for (const fn of [...listeners]) fn();
  } catch { /* the server is restarting (an update) - try again soon */ }
  timer = setTimeout(refreshUpdates, pollMs());
}

export function setUpdates(st) {
  updates = st;
  render();
  for (const fn of [...listeners]) fn();
  clearTimeout(timer);
  timer = setTimeout(refreshUpdates, pollMs());
}

function render() {
  if (!box) return;
  clear(box);
  for (const b of updates.banners || []) {
    const cls = b.level >= 4 ? "banner crit" : b.level >= 3 ? "banner" : b.kind === "result" ? "banner ok" : "banner info";
    const label = b.level >= 4 ? "Update failed:" : b.level >= 3 ? "Update:" : "Update:";
    const actions = h("span", { class: "actions" });
    if (b.kind === "available") actions.append(h("button", { class: "btn primary", type: "button", text: "Review update", onclick: openReview }));
    if (["result", "failed", "staged"].includes(b.kind) && admin.signedIn) {
      actions.append(h("button", { class: "btn", type: "button", text: "Dismiss",
        onclick: async () => setUpdates(await post("/api/admin/update/dismiss")) }));
    }
    if (b.kind === "installing" || b.kind === "result") actions.append(h("a", { href: "#/admin", text: "Details" }));
    box.append(h("div", { class: cls, role: b.level >= 3 ? "alert" : "status" }, h("b", { text: label }), h("span", { text: b.text }), actions));
  }
  if (footer) {
    const c = updates.check || {};
    setText(footer, c.at ? `Updates: ${c.ok ? "checked" : "can't check"} ${fmtWhen(c.at)}${c.ok ? "" : " (" + shortWhy(c.message) + ")"}`
      : "Updates: not checked yet");
    footer.title = c.message || "";
  }
}

function shortWhy(msg) {
  if (!msg) return "unknown";
  if (/rate limit/i.test(msg)) return "GitHub limit";
  if (/no internet/i.test(msg)) return "offline";
  return "see Admin";
}

// ---------------------------------------------------------------------------
export function signInForm(onDone) {
  const name = h("input", { type: "text", autocomplete: "username", placeholder: "Your name (for the event log)", "aria-label": "Your name", maxlength: 40 });
  const pw = h("input", { type: "password", autocomplete: "current-password", placeholder: "Admin password", "aria-label": "Admin password", required: true });
  const msg = h("div", { class: "form-msg", role: "alert" });
  const btn = h("button", { class: "btn primary", type: "submit", text: "Sign in" });
  const form = h("form", { class: "signin" }, name, pw, btn, msg);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();                 // fetch only: the page's CSP allows no form posts
    btn.disabled = true;
    setText(msg, "");
    try {
      await post("/api/admin/login", { password: pw.value, name: name.value });
      pw.value = "";
      await refreshSession();
      onDone?.();
    } catch (err) {
      setText(msg, err.message);
    } finally { btn.disabled = false; }
  });
  if (!admin.configured) {
    return h("p", { class: "muted", text: "No admin password is set on this server yet. On the server run: sudo bash deploy/install.sh --secrets" });
  }
  setTimeout(() => (admin.name ? pw : name).focus(), 0);
  const devNote = admin.deploy === "compose"
    ? "This test install uses a one-time password printed in its log: docker compose logs app (a new one each start)."
    : "This PC run uses a one-time password printed in the console where the dashboard was started.";
  return h("div", {}, admin.devPassword ? h("p", { class: "muted", text: devNote }) : null, form);
}

export async function openReview() {
  const st = updates;
  const v = st?.offered || st?.latest?.version;
  if (!v) return;
  const msg = h("div", { class: "form-msg", role: "alert" });
  const body = h("div");
  let close = null;
  const act = (what, label) => h("button", { class: what === "approve" ? "btn primary" : "btn", type: "button", text: label, onclick: async (ev) => {
    ev.target.disabled = true;
    setText(msg, "");
    try {
      setUpdates(await post(`/api/admin/update/${what}`));
      close();
    } catch (e) { setText(msg, e.message); ev.target.disabled = false; }
  } });
  const fill = () => {
    clear(body);
    const latest = st.latest || {};
    body.append(
      h("p", {}, `Running ${st.current}. `, h("b", { text: `Version ${v}` }),
        latest.published_at ? ` was published ${fmtWhen(Date.parse(latest.published_at) / 1000)}` : "",
        latest.size ? ` (${latest.size >= 1048576 ? (latest.size / 1048576).toFixed(1) + " MB" : Math.ceil(latest.size / 1024) + " KB"}).` : "."),
      h("h3", { text: "What's in it" }),
      h("pre", { class: "notes", text: latest.notes || "No release notes." }),
      st.deploy === "compose"
        ? h("p", { class: "muted", text: `This is a developer install from git, so it does not install updates itself. To update it: ${GIT_UPDATE}.` })
        : st.can_install
          ? h("p", { class: "muted", text: "Approve downloads it, checks its checksum and version, then the server rebuilds and restarts the dashboard (about a minute). If the new version does not come up healthy, the server goes back to this one by itself." })
          : h("p", { class: "muted", text: "This copy runs on a PC: Approve downloads and checks the update, but installing needs the site server (install.sh)." }));
    if (admin.signedIn) {
      body.append(h("div", { class: "actions-row" }, st.deploy === "compose" ? null : act("approve", `Approve ${v}`), act("remind", "Remind me tomorrow"), act("skip", "Skip this version")),
        h("p", { class: "muted small", text: `Signed in as ${admin.name}.` }));
    } else {
      body.append(h("h3", { text: "Only an admin can approve, postpone or skip an update" }), signInForm(fill));
    }
    body.append(msg);
  };
  fill();
  close = modal(`Update available: ${v}`, body);
}
