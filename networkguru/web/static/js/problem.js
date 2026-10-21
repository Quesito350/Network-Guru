// "Report a problem": the page's own errors are caught here (the button gets a red dot), and a
// signed-in admin downloads one zip with the server's status, events, log lines and these errors
// for whoever looks into it. Secrets never go in it.

import { errorText } from "./api.js";
import { h, setText } from "./dom.js";
import { modal } from "./modal.js";
import { admin, refreshSession, signInForm } from "./updates.js";

const MAX = 50;
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
  const btn = h("button", { class: "hbtn report", type: "button", title: "Download a report for whoever looks into a problem (admin)",
    onclick: openProblemReport }, "Report a problem", marker);
  onErrors((n) => {
    marker.classList.toggle("hidden", !n);
    btn.title = n ? `This page caught ${n} error${n > 1 ? "s" : ""} - include them in a report (admin)`
      : "Download a report for whoever looks into a problem (admin)";
  });
  return btn;
}

async function download(note) {
  const r = await fetch("/api/admin/problem-report", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "networkguru" },
    body: JSON.stringify({ note, browser: { url: location.href, agent: navigator.userAgent,
      screen: `${screen.width}x${screen.height} @${devicePixelRatio}`, errors: caught } }),
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

export async function openProblemReport() {
  await refreshSession().catch(() => {});
  const body = h("div");
  const close = modal("Report a problem", body);
  const fill = () => {
    body.replaceChildren();
    if (!admin.signedIn) {
      body.append(h("p", { text: "The report holds the site's device addresses, so only an admin can download it." }),
        signInForm(fill));
      return;
    }
    const note = h("textarea", { class: "wide", rows: 4, placeholder: "What happened, and roughly when? (optional)", "aria-label": "What happened" });
    const msg = h("div", { class: "form-msg", role: "status" });
    const go = h("button", { class: "btn primary", type: "button", text: "Download report", onclick: async () => {
      go.disabled = true;
      setText(msg, "Putting the report together…");
      try {
        const name = await download(note.value);
        setText(msg, `Saved as ${name}. Send it to whoever looks into the problem.`);
      } catch (e) { setText(msg, e.message); }
      go.disabled = false;
    } });
    body.append(
      h("p", { text: "One zip with this server's version, status, the last 7 days of events, recent log lines, "
        + "the inventory and any errors this page caught. Passwords, tokens and other secrets are never in it." }),
      note,
      h("p", { class: "muted small", text: caught.length ? `This page caught ${caught.length} error${caught.length > 1 ? "s" : ""}; they are included.`
        : "This page has not caught any errors." }),
      h("div", { class: "actions-row" }, go, h("button", { class: "btn", type: "button", text: "Close", onclick: () => close() })),
      msg);
    setTimeout(() => note.focus(), 0);
  };
  fill();
}
