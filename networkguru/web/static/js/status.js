// Status badges, dots and icons. Colour is never alone: every badge has an icon and a word.

import { h, s, setClass, setText } from "./dom.js";
import { STATUS_LABEL, statusName } from "./fmt.js";

const ICONS = {
  ok: () => [s("circle", { cx: 8, cy: 8, r: 7, fill: "var(--s-ok)" }),
    s("path", { d: "M4.6 8.2l2.2 2.2 4.6-4.8", fill: "none", stroke: "#fff", "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round" })],
  warning: () => [s("path", { d: "M8 1.2l7 12.6H1z", fill: "var(--s-warning)", "stroke-linejoin": "round" }),
    s("path", { d: "M8 5.6v4", stroke: "#111", "stroke-width": 1.7, "stroke-linecap": "round" }),
    s("circle", { cx: 8, cy: 11.9, r: 0.95, fill: "#111" })],
  critical: () => [s("circle", { cx: 8, cy: 8, r: 7, fill: "var(--s-critical)" }),
    s("path", { d: "M5.4 5.4l5.2 5.2M10.6 5.4l-5.2 5.2", stroke: "#fff", "stroke-width": 1.8, "stroke-linecap": "round" })],
  offline: () => [s("circle", { cx: 8, cy: 8, r: 7, fill: "var(--s-offline)" }),
    s("path", { d: "M4.8 8h6.4", stroke: "#fff", "stroke-width": 1.8, "stroke-linecap": "round" })],
  unknown: () => [s("circle", { cx: 8, cy: 8, r: 6.2, fill: "none", stroke: "var(--s-unknown)", "stroke-width": 1.6, "stroke-dasharray": "2.4 1.6" }),
    s("text", { x: 8, y: 11.3, "text-anchor": "middle", "font-size": 9, "font-weight": "bold", fill: "var(--muted)", "font-family": "Arial" }, "?")],
};

export function icon(name) {
  return s("svg", { viewBox: "0 0 16 16", "aria-hidden": "true" }, ICONS[name] ? ICONS[name]() : ICONS.unknown());
}

export function badge(st, { small = false } = {}) {
  const name = typeof st === "number" ? statusName(st) : st;
  const el = h("span", { class: `badge s-${name}${small ? " small" : ""}` }, icon(name), h("span", { text: STATUS_LABEL[name] }));
  return el;
}

export function updateBadge(el, st) {
  const name = statusName(st);
  if (el.dataset.s === name) return;
  el.dataset.s = name;
  setClass(el, "s-", name);
  el.replaceChildren(icon(name), h("span", { text: STATUS_LABEL[name] }));
}

export function dot(st, title) {
  const name = typeof st === "number" ? statusName(st) : st;
  return h("span", { class: `sdot s-${name}`, title: title || STATUS_LABEL[name], role: "img", "aria-label": STATUS_LABEL[name] });
}

export function updateDot(el, st, title) {
  const name = statusName(st);
  setClass(el, "s-", name);
  el.title = title || STATUS_LABEL[name];
  el.setAttribute("aria-label", STATUS_LABEL[name]);
}

export function countsLine(counts) {
  const order = ["critical", "warning", "offline", "unknown", "ok"];
  return order.filter((k) => counts?.[k]).map((k) => h("span", { class: "chip-count", title: STATUS_LABEL[k] }, dot(k), String(counts[k])));
}

export { setText };
