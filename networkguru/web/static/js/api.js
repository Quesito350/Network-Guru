// HTTP + WebSocket. The WebSocket reconnects by itself with a growing pause.

import { notify, store } from "./store.js";

export async function get(path, params = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) if (v != null && v !== "") url.searchParams.set(k, v);
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

// The server's own words for a refusal ({"detail": "..."}), so pages can show them as they are.
export async function errorText(r) {
  try {
    const j = await r.json();
    return typeof j.detail === "string" ? j.detail : `${r.status} ${JSON.stringify(j.detail ?? j)}`;
  } catch { return `${r.status} ${r.statusText}`; }
}

// Changes always carry X-Requested-With: the server refuses admin changes without it (another
// web site cannot send that header).
export async function post(path, body, method = "POST") {
  const r = await fetch(path, { method, headers: { "Content-Type": "application/json", "X-Requested-With": "networkguru" },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (!r.ok) {
    const e = new Error(await errorText(r));
    e.status = r.status;
    throw e;
  }
  return r.json();
}

let ws = null;
let backoff = 1000;

export function connectLive() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  store.ws = "connecting";
  notify("conn");
  ws = new WebSocket(`${proto}//${location.host}/ws/live`);
  ws.onopen = () => {
    backoff = 1000;
    store.ws = "connected";
    notify("conn");
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    store.lastMsg = Date.now() / 1000;
    if (msg.backfill != null) store.backfill = msg.backfill;
    if (msg.type === "snapshot") {
      store.nodes = msg.nodes;
    } else if (msg.type === "delta") {
      Object.assign(store.nodes, msg.nodes);
    }
    if (msg.version != null) store.version = msg.version;
    if (msg.ts != null) store.serverTime = msg.ts;
    if (msg.type !== "hb") notify("live");
  };
  ws.onclose = () => {
    store.ws = "down";
    notify("conn");
    setTimeout(connectLive, backoff);
    backoff = Math.min(backoff * 2, 15000);
  };
  ws.onerror = () => ws.close();
}
