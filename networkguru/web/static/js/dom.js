// Tiny DOM builder. Text always goes in as text nodes (names come from the inventory).

const SVGNS = "http://www.w3.org/2000/svg";

export function h(tag, attrs, ...children) {
  const isSvg = tag.startsWith("svg:");
  const el = isSvg ? document.createElementNS(SVGNS, tag.slice(4)) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "text") el.textContent = v;
    else if (k === "class") el.setAttribute("class", v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const s = (tag, attrs, ...children) => h("svg:" + tag, attrs, ...children);

export function clear(el) {
  while (el.firstChild) el.firstChild.remove();
  return el;
}

export function setText(el, text) {
  const t = text == null ? "" : String(text);
  if (el.textContent !== t) el.textContent = t;
}

export function setClass(el, prefix, value) {
  for (const c of [...el.classList]) if (c.startsWith(prefix)) el.classList.remove(c);
  if (value) el.classList.add(prefix + value);
}
