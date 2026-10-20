// A dialog over the page: Esc, the × and a click outside close it. Returns close().

import { h } from "./dom.js";

export function modal(title, body) {
  const scrim = h("div", { class: "scrim" });
  const box = h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": title },
    h("header", {}, h("h2", { text: title }), h("button", { class: "xbtn", type: "button", "aria-label": "Close", text: "×", onclick: () => close() })),
    h("div", { class: "body" }, body));
  const close = () => { scrim.remove(); box.remove(); document.removeEventListener("keydown", esc); };
  const esc = (e) => { if (e.key === "Escape") close(); };
  scrim.addEventListener("click", close);
  document.addEventListener("keydown", esc);
  document.body.append(scrim, box);
  box.querySelector(".xbtn").focus();
  return close;
}
