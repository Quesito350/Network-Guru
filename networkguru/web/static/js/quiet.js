// Live's "gone quiet" rule (0.8.0): a group none of whose devices has answered for the admin's hide
// time (Admin → Live view, default 60 min) drops off the Live overview and the map, with a switch
// to show it again. History, the group's own page, alerts and the morning report still have it.
// Fresh Mobius data for the group counts as an answer; a group nothing checks is never hidden.

import { get } from "./api.js";
import { node, notify, store } from "./store.js";

function lsGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } }

// When anything of the group last answered (seconds), or null.
export function lastHeard(g) {
  let t = null;
  for (const c of g.components) {
    const s = node(c.id).seen;
    if (s && (t == null || s > t)) t = s;
  }
  const m = store.mobius?.groups?.[g.id];
  if (m && (t == null || m > t)) t = m;
  return t;
}

// The hide rule for one group. `always` = apply it outside Live too (the map shows the live state).
export function isQuiet(g, { always = false, now = Date.now() / 1000 } = {}) {
  if (!always && store.mode !== "live") return false;
  const hq = store.meta?.hide_quiet || {};
  if (!hq.min || !(hq.tabs || []).includes(g.tab)) return false;
  if (!g.components.some((c) => node(c.id).poll != null || node(c.id).seen != null)) return false;  // not checked at all
  const since = lastHeard(g) ?? store.meta.started ?? now;
  return now - since > hq.min * 60;
}

export function quietText(g) {
  const t = lastHeard(g);
  if (!t) return "no answer since the dashboard started";
  const d = new Date(t * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return `no answer since ${d.toLocaleString([], today ? { hour: "2-digit", minute: "2-digit" }
    : { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`;
}

export const showHidden = (where) => lsGet(`ng_show_hidden_${where}`) === "1";
export function setShowHidden(where, on) { lsSet(`ng_show_hidden_${where}`, on ? "1" : "0"); }

// Mobius data times per group (and positions), when the server runs the Mobius explorer.
let polling = false;
export function startMobiusSummary() {
  if (polling || !store.meta?.mobius_explore) return;
  polling = true;
  const once = async () => {
    try {
      store.mobius = await get("/api/mobius/summary");
      notify("live");
    } catch (e) { console.error(e); }
  };
  once();
  setInterval(once, 30000);
}
