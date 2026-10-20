// App state: the inventory, live node states from the WebSocket, and the Live/History choice.

export const store = {
  meta: null,
  inv: null,
  nodes: {},
  version: 0,
  serverTime: null,
  lastMsg: 0,
  ws: "connecting",
  backfill: 100,
  mode: "live",          // live | history
  range: "24h",          // 1h | 24h | 7d | 30d | custom
  from: null,            // custom range (epoch s)
  to: null,
  groups: {},            // id -> group (+ tab)
  comps: {},             // id -> component (+ group)
};

export const RANGES = { "1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400 };
export const LIVE_WINDOW = 3600;

const subs = new Set();
let queued = false;

export function subscribe(fn) {
  subs.add(fn);
  return () => subs.delete(fn);
}

export function notify(kind = "live") {
  if (queued) return;
  queued = true;
  requestAnimationFrame(() => {
    queued = false;
    for (const fn of [...subs]) {
      try { fn(kind); } catch (e) { console.error(e); }
    }
  });
}

export function indexInventory(inv) {
  store.inv = inv;
  store.groups = {};
  store.comps = {};
  for (const tab of inv.tabs) {
    for (const g of tab.groups) {
      store.groups[g.id] = { ...g, tab: tab.id };
      for (const c of g.components) store.comps[c.id] = { ...c, group: g.id, tab: tab.id };
    }
  }
}

export const node = (id) => store.nodes[id] || { s: 1, r: ["Waiting for data"], m: {}, i: {} };

// The time window everything below the header shows.
export function window_() {
  const now = Date.now() / 1000;
  if (store.mode === "live") return { from: now - LIVE_WINDOW, to: now, live: true };
  if (store.range === "custom" && store.from && store.to) return { from: store.from, to: Math.min(store.to, now), live: false };
  return { from: now - (RANGES[store.range] || 86400), to: now, live: false };
}

export function rangeLabel() {
  if (store.mode === "live") return "last hour";
  if (store.range === "custom") return "custom range";
  return { "1h": "last hour", "24h": "last 24 hours", "7d": "last 7 days", "30d": "last 30 days" }[store.range];
}
