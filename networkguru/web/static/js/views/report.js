// Morning report preview: the e-mail as it would go out right now (the Teams card carries the
// same lines), plus the plain-text version and when it was last sent. The e-mail HTML is built
// by our own server (every name escaped) and shown in a shadow root, so its inline styles stay
// inside it; the page's CSP still blocks any script. /report/preview.html shows it on its own.

import { get, post } from "../api.js";
import { clear, h, setText } from "../dom.js";
import { admin, refreshSession } from "../updates.js";

export function reportView(root) {
  const sub = h("span", { class: "sub" });
  const frame = h("div", { class: "report-paper", role: "document", "aria-label": "Morning report preview" });
  const shadow = frame.attachShadow({ mode: "open" });
  const text = h("pre", { class: "report-text hidden" });
  const sent = h("div", { class: "muted" });
  const tabs = h("div", { class: "seg light", role: "group", "aria-label": "Preview format" });
  const show = (which) => {
    frame.classList.toggle("hidden", which !== "html");
    text.classList.toggle("hidden", which !== "text");
    for (const b of tabs.children) b.setAttribute("aria-pressed", String(b.dataset.v === which));
  };
  for (const [v, t] of [["html", "E-mail / Teams"], ["text", "Plain text"]]) {
    tabs.append(h("button", { type: "button", "data-v": v, text: t, onclick: () => show(v) }));
  }
  const msg = h("div", { class: "form-msg" });
  const actions = h("div", { class: "actions-row" }, tabs,
    h("button", { class: "btn", type: "button", text: "Refresh", onclick: () => load() }),
    h("a", { href: "/report/preview.html", target: "_blank", rel: "noopener", text: "Open as a page" }));
  root.append(h("div", { class: "page-head" }, h("h1", { text: "Morning report" }), sub), actions, sent, msg, frame, text);

  async function load() {
    clear(msg);
    try {
      const r = await get("/api/report/preview");
      const st = r.settings;
      setText(sub, `As it would be sent now · goes out every day at ${st.report_time} (site time)` +
        (st.report_teams || st.report_email ? ` to ${[st.report_teams ? "Teams" : "", st.report_email ? "e-mail" : ""].filter(Boolean).join(" and ")}` : " - no channel switched on"));
      setText(text, r.text);
      const chans = Object.entries(r.sent || {});
      setText(sent, !r.sent_date ? "Not sent yet."
        : chans.length ? `Last report ${r.sent_date}: ` + chans.map(([k, v]) => `${k} ${v.ok ? "✓" : "✗"} ${v.message || ""}`).join(", ")
          : `Last report ${r.sent_date}: not sent - ${r.sent_note || "no channel set up"} (Admin page).`);
      const doc = new DOMParser().parseFromString(r.html, "text/html");
      shadow.replaceChildren(...[...doc.body.childNodes].map((n) => document.importNode(n, true)));
    } catch (e) { setText(msg, e.message); }
    await refreshSession().catch(() => {});
    actions.querySelector(".test-btn")?.remove();
    if (admin.signedIn) {
      const b = h("button", { class: "btn test-btn", type: "button", text: "Send a test now", onclick: async () => {
        b.disabled = true;
        setText(msg, "Sending…");
        try {
          const res = await post("/api/admin/report/test");
          setText(msg, Object.entries(res).map(([k, v]) => `${k}: ${v}`).join(" · "));
        } catch (e) { setText(msg, e.message); }
        b.disabled = false;
      } });
      actions.append(b);
    } else {
      msg.append(h("span", { class: "muted" }, "Sign in on the ", h("a", { href: "#/admin", text: "Admin page" }), " to send a test."));
    }
  }
  show("html");
  load();
  return {};
}
