// "Report a problem": the page's own errors are caught here (the button gets a red dot).
// 0.10.0: anyone can report a problem. The server keeps the full report (status, events, log lines and
// these errors - never secrets) and hands back a ticket for the site's Network Change Requests page
// (problem_ticket.py): that page opens in a new tab, filled in, with an error-log excerpt; the person
// picks their team and presses Submit there. The same ticket is copied, for the page's "Paste from
// Network Guru" box. An admin can also download the full zip.

import { errorText, post } from "./api.js";
import { h, setText } from "./dom.js";
import { modal } from "./modal.js";
import { store } from "./store.js";
import { admin, refreshSession } from "./updates.js";

const MAX = 50;
const PRIORITIES = ["Standard", "High", "Urgent", "Emergency"];
const caught = [];
const listeners = new Set();

function keep(e) {
  caught.push({ time: new Date().toISOString(), page: location.hash, ...e });
  if (caught.length > MAX) caught.shift();
  for (const fn of listeners) fn(caught.length);
}

export function watchErrors() {
  window.addEventListener("error", (ev) => {
    keep({ message: String(ev.message || ev.error || "error"), source: `${ev.filename || ""}:${ev.lineno || 0}:${ev.colno || 0}`,
      stack: String(ev.error?.stack || "").slice(0, 2000) });
  });
  window.addEventListener("unhandledrejection", (ev) => {
    const r = ev.reason;
    keep({ message: `Unhandled: ${String(r?.message || r)}`, stack: String(r?.stack || "").slice(0, 2000) });
  });
}

export function onErrors(fn) { listeners.add(fn); fn(caught.length); return () => listeners.delete(fn); }

export function errorButton() {
  const marker = h("span", { class: "err-dot hidden", title: "This page caught an error" });
  const plain = "Tell Support something is wrong - it becomes a network change request with this dashboard's error log";
  const btn = h("button", { class: "hbtn report", type: "button", title: plain, onclick: openProblemReport }, "Report a problem", marker);
  onErrors((n) => {
    marker.classList.toggle("hidden", !n);
    btn.title = n ? `This page caught ${n} error${n > 1 ? "s" : ""} - they go in the report` : plain;
  });
  return btn;
}

const browserInfo = () => ({ url: location.href, agent: navigator.userAgent,
  screen: `${screen.width}x${screen.height} @${devicePixelRatio}`, errors: caught });

async function download(note) {
  const r = await fetch("/api/admin/problem-report", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "networkguru" },
    body: JSON.stringify({ note, browser: browserInfo() }),
  });
  if (!r.ok) throw new Error(await errorText(r));
  const name = /filename="([^"]+)"/.exec(r.headers.get("Content-Disposition") || "")?.[1] || "networkguru-report.zip";
  const url = URL.createObjectURL(await r.blob());
  const a = h("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  return name;
}

// The clipboard API needs https; the dashboard is often plain http, so the old way (inside the click).
function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text);
      return true;
    }
    const ta = h("textarea", { style: "position:fixed;left:-9999px;top:0", "aria-hidden": "true" });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch { return false; }
}

export async function openProblemReport() {
  const ncr = Boolean(store.meta?.ncr);
  const body = h("div");
  const close = modal("Report a problem", body);
  const text = h("textarea", { class: "wide", rows: 5, placeholder: "What's wrong, where, and since when?", "aria-label": "What's wrong" });
  const prio = h("select", { class: "btn", "aria-label": "Priority" }, ...PRIORITIES.map((p) => h("option", { value: p, text: p })));
  const emerg = h("p", { class: "form-msg hidden", text: "Emergency: also call Support now - a ticket alone is not enough." });
  prio.addEventListener("change", () => emerg.classList.toggle("hidden", prio.value !== "Emergency"));
  const msg = h("div", { class: "form-msg", role: "status" });
  const go = h("button", { class: "btn primary", type: "button", text: ncr ? "Create ticket" : "Send report", onclick: () => send() });
  const zip = h("button", { class: "btn hidden", type: "button", text: "Download the full report (zip)", title: "Admins only",
    onclick: async () => {
      zip.disabled = true;
      try { setText(msg, `Saved as ${await download(text.value)}.`); } catch (e) { setText(msg, e.message); }
      zip.disabled = false;
    } });
  const showZip = () => zip.classList.toggle("hidden", !admin.signedIn);
  refreshSession().then(showZip, showZip);

  async function send() {
    if (!text.value.trim()) {
      setText(msg, "Say what's wrong first.");
      text.focus();
      return;
    }
    // open the tab inside the click, or the browser blocks it; it goes to the page once the ticket is ready
    const tab = ncr ? window.open("", "_blank") : null;
    go.disabled = true;
    setText(msg, "Putting the report together…");
    let res;
    try {
      res = await post("/api/problem", { text: text.value, priority: prio.value, page: location.hash, browser: browserInfo() });
    } catch (e) {
      tab?.close();
      setText(msg, e.message);
      go.disabled = false;
      return;
    }
    const copied = res.ng ? copyText(`ng=${res.ng}`) : false;
    let opened = false;
    if (tab && res.url) {
      try {
        tab.opener = null;
        tab.location.href = res.url;
        opened = true;
      } catch { /* the person closed it */ }
    } else tab?.close();
    done(res, copied, opened);
  }

  function done(res, copied, opened) {
    const kb = (res.log_chars / 1024).toFixed(1);
    const parts = [h("p", {}, "Report ", h("b", { text: res.id }), " is kept on this server for the admins.")];
    if (res.url) {
      parts.push(
        h("p", { text: (opened ? "The Network Change Requests page opened in a new tab" : "Open the Network Change Requests page:")
          + ` the ticket is filled in, with this dashboard's error log attached (${kb} KB). Pick your team there and press Submit request.` }),
        h("div", { class: "actions-row" },
          h("a", { class: "btn primary", href: res.url, target: "_blank", rel: "noopener", text: opened ? "Open it again" : "Open the change request page" }),
          h("button", { class: "btn", type: "button", text: "Copy the ticket", onclick: (e) => {
            setText(e.target, copyText(`ng=${res.ng}`) ? "Copied" : "Copy didn't work");
          } })),
        h("p", { class: "muted small", text: (copied ? "The ticket is also copied. " : "")
          + "If the page opens empty, use its 'Paste from Network Guru' box and paste (Ctrl+V). You need to be signed in to Claude." }));
    } else {
      parts.push(h("p", { text: "This dashboard's admins see it under Admin → Problem reports. For it to become a network change "
        + "request, an admin sets the change request page under Admin → Settings." }));
    }
    parts.push(h("div", { class: "actions-row" }, zip, h("button", { class: "btn", type: "button", text: "Close", onclick: () => close() })));
    body.replaceChildren(...parts);
    showZip();
  }

  body.append(
    h("p", { text: ncr
      ? "Tell Support what's wrong. This opens the Network Change Requests page with a ticket filled in and this "
        + "dashboard's error log attached - pick your team there and press Submit request (signed in to Claude)."
      : "Tell the admins what's wrong. The report is kept on this server with this dashboard's error log." }),
    text,
    h("div", { class: "actions-row" }, h("label", {}, "Priority ", prio)),
    emerg,
    h("p", { class: "muted small", text: (caught.length ? `This page caught ${caught.length} error${caught.length > 1 ? "s" : ""}; they go in the report. `
      : "") + "Passwords, tokens and other secrets are never in it." }),
    h("div", { class: "actions-row" }, go, zip, h("button", { class: "btn", type: "button", text: "Cancel", onclick: () => close() })),
    msg);
  setTimeout(() => text.focus(), 0);
}
