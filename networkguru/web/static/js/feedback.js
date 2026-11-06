// Feedback (0.9.5): anyone viewing sends a request, a problem or an idea - no sign-in. Signed-in admins
// track them on the Admin page (views/admin.js); the morning report lists the new and open ones.

import { post } from "./api.js";
import { h, setText } from "./dom.js";
import { modal } from "./modal.js";

const KINDS = [["request", "Request"], ["problem", "Problem"], ["idea", "Idea"]];
const MAX = 2000;

function lsGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } }

export function feedbackButton() {
  return h("button", { class: "hbtn", type: "button", text: "Feedback", title: "Send a request, problem or idea to the dashboard's admins",
    onclick: openFeedback });
}

export function openFeedback() {
  const page = location.hash || "#/overview";
  const where = (document.title || "").split(" · ")[0] || page;
  const body = h("div");
  const close = modal("Feedback", body);
  const kind = h("select", { class: "btn", "aria-label": "Kind" }, ...KINDS.map(([v, t]) => h("option", { value: v, text: t })));
  const text = h("textarea", { class: "wide", rows: 5, maxlength: MAX, "aria-label": "Your feedback",
    placeholder: "What would help, what is missing, or what is wrong?" });
  const left = h("small", { class: "muted" });
  const name = h("input", { type: "text", maxlength: 60, value: lsGet("ng_feedback_name") || "", placeholder: "optional", "aria-label": "Your name" });
  const msg = h("div", { class: "form-msg", role: "status" });
  const count = () => setText(left, text.value.length > MAX - 200 ? `${MAX - text.value.length} characters left` : "");
  text.addEventListener("input", count);
  const send = h("button", { class: "btn primary", type: "button", text: "Send", onclick: async () => {
    if (!text.value.trim()) { setText(msg, "Write what you would like, or what is wrong."); text.focus(); return; }
    send.disabled = true;
    setText(msg, "Sending…");
    try {
      const r = await post("/api/feedback", { kind: kind.value, text: text.value, name: name.value, page });
      lsSet("ng_feedback_name", name.value.trim());
      body.replaceChildren(
        h("p", {}, h("b", { text: `Thanks - sent as #${r.id}.` })),
        h("p", { class: "muted", text: "The admins see it on the Admin page and in the morning report." }),
        h("div", { class: "actions-row" }, h("button", { class: "btn", type: "button", text: "Close", onclick: () => close() })));
    } catch (e) { setText(msg, e.message); send.disabled = false; }
  } });
  body.append(
    h("p", { text: "Goes to the dashboard's admins. They see it on the Admin page, and new ones are in the morning report." }),
    h("div", { class: "fields" }, h("label", { class: "field" }, h("span", { text: "What kind" }), kind),
      h("label", { class: "field" }, h("span", { text: "Your name" }), name)),
    text, left,
    h("p", { class: "muted small", text: `Sent from: ${where}. Please don't put passwords or other logins in it.` }),
    h("div", { class: "actions-row" }, send, h("button", { class: "btn", type: "button", text: "Cancel", onclick: () => close() })),
    msg);
  setTimeout(() => text.focus(), 0);
}
