// Problems now, and toasts for new ones (0.9.9).
//
// problemRows(): every device and place that is Warning or Critical now, across the three tabs - the rows the
// Overview's old "Needs attention now" list had (common.js attentionRows): a device hidden behind a failed one
// and a place gone quiet (quiet.js) are left out. The Errors button counts them and its drawer lists them.
//
// Toasts: a device or place that newly turns Warning or Critical - or gets worse - pops up at the top right for
// 30 s, on every page; hovering pauses it, a click opens it. No storms: when the page opens (and for its first
// few seconds, while the Mobius times arrive) one summary toast says how many need attention; the same one again
// within 5 minutes only if it got worse; at most 4 at once - the rest fold into "+N more".

import { h, setText } from "./dom.js";
import { fmtWhen, statusName } from "./fmt.js";
import { isQuiet } from "./quiet.js";
import { badge } from "./status.js";
import { store, subscribe } from "./store.js";
import { attentionRows } from "./views/common.js";

const SHOW_MS = 30000;           // each toast
const AGAIN_S = 300;             // the same device again only after this (unless it got worse)
const MAX_SHOWN = 4;
const SETTLE_MS = 10000;         // the page's first seconds: counted in the summary, not toasted one by one

export function siteGroups() {
  return (store.inv?.tabs || []).filter((t) => t.id !== "map").flatMap((t) => (t.groups || []).map((g) => store.groups[g.id]))
    .filter(Boolean);
}

export function problemRows() {
  const groups = siteGroups();
  const quiet = new Set(groups.filter((g) => isQuiet(g, { always: true })).map((g) => g.id));
  return attentionRows(groups, quiet);
}

const rowKey = (r) => (r.c ? r.c.id : r.g.id);
const rowHref = (r) => (r.c ? `#/c/${r.c.id}` : `#/${r.g.tab}/${r.g.id}`);
const rowTitle = (r) => (r.c ? `${r.g.name} · ${r.c.name}` : r.g.name);

export function startToasts({ nav, openErrors }) {
  const stack = h("div", { class: "toasts", role: "region", "aria-label": "New problems", "aria-live": "polite" });
  document.body.append(stack);
  const present = new Map();     // key -> status in the last update
  const told = new Map();        // key -> { s, t } when last toasted
  const t0 = performance.now();
  let summary = null;            // the "N need attention" toast while the page settles
  let more = null;               // the "+N more" toast
  let moreN = 0;

  function toast({ st, title, why, when, href, onOpen }) {
    const bar = h("div", { class: "toast-bar" });
    const titleEl = h("b", { text: title });
    const whyEl = h("span", { class: "toast-why", text: why || "" });
    const open = h("a", { class: "toast-open", href: href || "#",
      onclick: (e) => { e.preventDefault(); close(); onOpen(); } },
    titleEl, whyEl, when ? h("span", { class: "toast-when muted", text: when }) : null);
    let b = badge(st, { small: true });
    const el = h("div", { class: `toast s-${statusName(st)}` }, b, open,
      h("button", { type: "button", class: "xbtn", "aria-label": "Close", text: "×", onclick: () => close() }), bar);
    let left = SHOW_MS, started = performance.now(), timer = setTimeout(close, left);
    const pause = () => {
      if (el.classList.contains("paused")) return;
      clearTimeout(timer);
      left = Math.max(1000, left - (performance.now() - started));
      el.classList.add("paused");
    };
    const resume = () => {
      if (!el.classList.contains("paused") || el.matches(":hover") || el.contains(document.activeElement)) return;
      el.classList.remove("paused");
      started = performance.now();
      timer = setTimeout(close, left);
    };
    el.addEventListener("mouseenter", pause);
    el.addEventListener("focusin", pause);
    el.addEventListener("mouseleave", resume);
    el.addEventListener("focusout", () => setTimeout(resume, 0));
    function close() {
      clearTimeout(timer);
      el.remove();
      if (t === more) { more = null; moreN = 0; }
      if (t === summary) summary = null;
    }
    function restart() {                         // a changed summary / "+N more": its 30 s start again
      clearTimeout(timer);
      left = SHOW_MS;
      started = performance.now();
      bar.style.animation = "none";
      void bar.offsetWidth;
      bar.style.animation = "";
      if (!el.classList.contains("paused")) timer = setTimeout(close, left);
    }
    const t = { el, close, restart, set(st2, title2, why2) {
      const nb = badge(st2, { small: true });
      b.replaceWith(nb);
      b = nb;
      setText(titleEl, title2);
      setText(whyEl, why2);
      el.className = `toast s-${statusName(st2)}${el.classList.contains("paused") ? " paused" : ""}`;
      restart();
    } };
    return t;
  }

  function place(t) {                            // newest on top; past MAX_SHOWN, the "+N more" toast
    const shown = stack.querySelectorAll(".toast:not(.more)").length;
    if (shown < MAX_SHOWN) {
      stack.prepend(t.el);
      return true;
    }
    return false;
  }

  function addMore(st) {
    moreN += 1;
    const title = `+${moreN} more`;
    if (!more) {
      more = toast({ st, title, why: "Errors lists every one", href: "#", onOpen: openErrors });
      more.el.classList.add("more");
      stack.append(more.el);
    } else more.set(Math.max(st, more.st || 0), title, "Errors lists every one");
    more.st = Math.max(st, more.st || 0);
  }

  function showRow(r) {
    const t = toast({ st: r.n.s, title: rowTitle(r), why: (r.n.r || []).join(" · "),
      when: r.n.since ? `since ${fmtWhen(r.n.since)}` : "", href: rowHref(r), onOpen: () => nav(rowHref(r)) });
    if (!place(t)) addMore(r.n.s);
  }

  function showSummary(rows) {
    const worst = Math.max(...rows.map((r) => r.n.s));
    const title = `${rows.length} need${rows.length === 1 ? "s" : ""} attention`;
    if (summary) {
      if (summary.key !== `${worst}|${title}`) summary.set(worst, title, "Open Errors to see them");   // changed: 30 s again
    } else {
      summary = toast({ st: worst, title, why: "Open Errors to see them", href: "#", onOpen: openErrors });
      stack.prepend(summary.el);
    }
    summary.key = `${worst}|${title}`;
  }

  function check() {
    if (!store.inv) return;
    const rows = problemRows();
    const now = Date.now() / 1000;
    const settling = performance.now() - t0 < SETTLE_MS;
    const seen = new Map();
    for (const r of rows) {
      const key = rowKey(r);
      seen.set(key, r.n.s);
      const was = present.get(key);
      if (was !== undefined && r.n.s <= was) continue;           // not new, not worse
      const last = told.get(key);
      if (settling) { told.set(key, { s: r.n.s, t: now }); continue; }
      if (last && now - last.t < AGAIN_S && r.n.s <= last.s) continue;
      told.set(key, { s: r.n.s, t: now });
      showRow(r);
    }
    present.clear();
    for (const [k, v] of seen) present.set(k, v);
    if (settling && rows.length) showSummary(rows);
    else if (settling && summary) summary.close();
  }

  subscribe((kind) => { if (kind === "live") check(); });
  check();
}
