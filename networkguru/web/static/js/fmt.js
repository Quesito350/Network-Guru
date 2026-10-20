// Number, unit, time and status formatting. Times are shown in the site's time zone.

export const STATUS = ["ok", "unknown", "offline", "warning", "critical"];
export const STATUS_LABEL = { ok: "OK", unknown: "Unknown", offline: "Offline", warning: "Warning", critical: "Critical" };

let TZ = "UTC";
let CAT = { metrics: {}, types: {} };
let THRESH = {};
let ENUMRULES = {};
const fmtCache = new Map();

export function setup(meta) {
  TZ = meta.site.timezone || "UTC";
  CAT = meta.catalog;
  THRESH = meta.thresholds || {};
  ENUMRULES = meta.enum_rules || {};
}

export const tz = () => TZ;
export const catalog = () => CAT;
export const statusName = (s) => STATUS[s] ?? "unknown";
export const metricSpec = (name) => CAT.metrics[name.split("{")[0]] || { label: name, unit: "", dec: 0 };
export const typeSpec = (key) => CAT.types[key] || CAT.types.host;
export const thresholds = (name) => THRESH[name] || null;

// A metric value's own health, for the small dots next to readings.
export function metricLevel(name, v) {
  if (v == null) return null;
  const er = ENUMRULES[name];
  if (er) {
    const lvl = er[String(Math.round(v))] ?? er["*"] ?? "ok";
    return lvl === "ok" ? null : lvl;
  }
  const t = THRESH[name];
  if (!t) return null;
  if ((t.crit != null && v >= t.crit) || (t.low_crit != null && v <= t.low_crit)) return "critical";
  if ((t.warn != null && v >= t.warn) || (t.low_warn != null && v <= t.low_warn)) return "warning";
  return null;
}

export function stateLevel(name, code) {
  const er = ENUMRULES[name];
  if (!er) return null;
  return er[String(Math.round(code))] ?? er["*"] ?? "ok";
}

function num(v, dec) {
  return Number(v).toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

// bps -> best unit for a whole chart
export function bpsScale(maxV) {
  if (maxV >= 1e9) return { div: 1e9, unit: "Gbps" };
  if (maxV >= 1e6) return { div: 1e6, unit: "Mbps" };
  if (maxV >= 1e3) return { div: 1e3, unit: "kbps" };
  return { div: 1, unit: "bps" };
}

export function fmtValue(name, v, { unit = true } = {}) {
  if (v == null || Number.isNaN(v)) return "–";
  const sp = metricSpec(name);
  if (sp.enum) return sp.enum[String(Math.round(v))] ?? String(v);
  let u = sp.unit;
  let dec = sp.dec;
  if (u === "bps") {
    const sc = bpsScale(Math.abs(v));
    v = v / sc.div;
    u = sc.unit;
    dec = v >= 100 ? 0 : 1;
  } else if (u === "s" && v >= 120) {
    if (v >= 172800) { v = v / 86400; u = "d"; dec = 0; }
    else if (v >= 7200) { v = v / 3600; u = "h"; dec = 1; }
    else { v = v / 60; u = "min"; dec = 0; }
  }
  const n = num(v, dec);
  if (!unit || !u) return n;
  return u === "%" || u === "°C" ? n + u : n + " " + u;
}

function dtf(opts) {
  const key = JSON.stringify(opts) + TZ;
  if (!fmtCache.has(key)) fmtCache.set(key, new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hourCycle: "h23", ...opts }));
  return fmtCache.get(key);
}

export function fmtTime(ts, style = "hm") {
  if (ts == null) return "–";
  const d = new Date(ts * 1000);
  switch (style) {
    case "hms": return dtf({ hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(d);
    case "day": return dtf({ weekday: "short", day: "numeric", month: "short" }).format(d);
    case "date": return dtf({ day: "numeric", month: "short" }).format(d);
    case "full": return dtf({ weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(d);
    case "fulls": return dtf({ weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(d);
    default: return dtf({ hour: "2-digit", minute: "2-digit" }).format(d);
  }
}

// "14:05" today, "Mon 12 Oct 14:05" otherwise
export function fmtWhen(ts, now = Date.now() / 1000) {
  if (ts == null) return "–";
  return fmtTime(ts, "date") === fmtTime(now, "date") ? fmtTime(ts) : fmtTime(ts, "full");
}

export function fmtDuration(sec) {
  if (sec == null) return "–";
  sec = Math.max(0, sec);
  if (sec < 90) return `${Math.round(sec)} s`;
  if (sec < 5400) return `${Math.round(sec / 60)} min`;
  if (sec < 172800) return `${(sec / 3600).toFixed(1)} h`;
  return `${(sec / 86400).toFixed(1)} days`;
}

export const fmtAgo = (sec) => (sec == null ? "–" : sec < 5 ? "just now" : `${fmtDuration(sec)} ago`);

export function fmtPct(v) {
  if (v == null) return "–";
  if (v >= 99.95) return "100%";
  return (v >= 99 ? v.toFixed(2) : v.toFixed(1)) + "%";
}

// Local-zone parts of a timestamp, for datetime-local inputs.
export function toLocalInput(ts) {
  const p = Object.fromEntries(dtf({ year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
    .formatToParts(new Date(ts * 1000)).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

// Inverse of toLocalInput: a site-local wall time -> epoch seconds (handles DST).
export function fromLocalInput(str) {
  const [d, t] = str.split("T");
  const [Y, M, D] = d.split("-").map(Number);
  const [hh, mm] = t.split(":").map(Number);
  let guess = Date.UTC(Y, M - 1, D, hh, mm) / 1000;
  for (let i = 0; i < 3; i++) {
    const shown = toLocalInput(guess);
    const [d2, t2] = shown.split("T");
    const [Y2, M2, D2] = d2.split("-").map(Number);
    const [h2, m2] = t2.split(":").map(Number);
    const diff = (Date.UTC(Y, M - 1, D, hh, mm) - Date.UTC(Y2, M2 - 1, D2, h2, m2)) / 1000;
    if (diff === 0) break;
    guess += diff;
  }
  return guess;
}
