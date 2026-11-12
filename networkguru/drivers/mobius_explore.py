"""Everything the Mobius InfluxDB has, shown on the vehicle / location / client pages (0.8.0; 0.11.0: all of it).

A side reader, not a component poller: it never changes a status. In live mode, when the site has a
Mobius InfluxDB URL:

    every 30 min   the layout - measurements, field keys, tag keys - and, per measurement, the "who"
                   tag: the configured asset_tag, else the tag whose values match inventory names
                   (Mobius asset, group name, device names, Mobius machine ID), else an asset-like tag
    every 2 min    a pass over every measurement, one query at a time with a pause between (gentle on the
                   Mobius server): the newest value of every field, per series (`SELECT * ... GROUP BY *
                   ORDER BY time DESC LIMIT 3`) - the first pass looks back a day, later ones 10 minutes -
                   merged into what is kept, so a parked vehicle keeps its last values (with their age)

0.11.0 (Mendon: 760 measurements, 1,579 fields): every measurement (up to 1,000, was 60), every field's
last value kept for a week (up to 3,000 per group, was 300 of the last 10 minutes), and `history()` - one
field's mean over 1 h - 7 d for its chart, on demand, for a value the explorer has seen.

What a "who" value does not match is kept as "Mobius knows, not in the inventory" - that is how
vehicles and client PCs missing from the sheet show up. A vehicle with lat / lon fields gets its map
pin from here. Read-only SELECT / SHOW only; fields named like passwords, tokens or keys are dropped,
text is cut to 200 characters, and problem reports get counts only, never values.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from ..config import secret
from .influx import ASSET_LIKE, PEOPLE_LIKE, InfluxClient, InfluxError, find_database, forget_database, q_ident, q_str
from .mobius_map import map_tables, mobius_map

log = logging.getLogger("networkguru.drivers")

PASS_S = 120.0                 # a pass over every measurement starts at most this often
PAUSE_S = 0.1                  # between two queries of a pass
SCHEMA_EVERY_S = 1800.0
BACKOFF_S = 300.0
WINDOW = "10m"
FIRST_WINDOW = "24h"           # the first pass after a start
TAG_WINDOW = "1d"
MAX_MEASUREMENTS = 1000        # the mapped ones first (mobius_map), then the rest by name
MAX_ROWS = 3000                # values kept per group (the newest)
KEEP_S = 7 * 86400.0           # a value not seen again for this long goes
MAX_TEXT = 200
SECRET_LIKE = re.compile(r"pass|secret|token|key|credential|auth", re.IGNORECASE)
LAT = re.compile(r"^(lat|latitude)$", re.IGNORECASE)
LON = re.compile(r"^(lon|lng|long|longitude)$", re.IGNORECASE)
SPANS = {"1h": "10s", "6h": "1m", "24h": "4m", "7d": "30m"}     # a history chart's span -> its bucket
MAX_POINTS = 400
HISTORY_CACHE_S = 60.0


def norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


class MobiusExplorer:
    def __init__(self, inv, timeout: float = 10.0, pause: float = PAUSE_S):
        cfg = inv.data.mobius
        ex = cfg.model_extra or {}
        self.inv = inv
        self.url, self.database, self.asset_tag = cfg.influx_url, cfg.database, cfg.asset_tag
        self.user_ref, self.password_ref = ex.get("user_ref") or "", ex.get("password_ref") or ""
        self.timeout = timeout
        self.pause = pause
        self.names = self._names(inv)
        self.mapped = map_tables(mobius_map(inv))
        self.schema: dict[tuple[str, str], dict] = {}          # (db, measurement) -> {"fields", "tags", "who"}
        self.schema_at = -1e18
        self.kept: dict[str, dict[tuple[str, str], dict]] = {}  # gid -> {(measurement, field): row}
        self.positions: dict[str, tuple[float, float, float]] = {}
        self.unmatched: dict[str, dict] = {}
        self.site_rows: dict[tuple[str, str], dict] = {}
        self.passes = 0
        self.pass_s: float | None = None                       # how long the last pass took
        self.error: str | None = None
        self.last_ok: float | None = None
        self._history: dict[tuple, tuple[float, dict]] = {}

    @staticmethod
    def _names(inv) -> dict[str, str]:
        """Normalised name -> group id. Vehicles' Mobius asset first, then group names, then devices."""
        out: dict[str, str] = {}
        groups = list(inv.groups.values())
        for g in groups:
            if g.mobius_asset:
                out.setdefault(norm(g.mobius_asset), g.id)
        for g in groups:
            out.setdefault(norm(g.name), g.id)
        for g in groups:
            for c in g.components:
                for n in (c.mobius_machine_id, c.name, c.hostname):
                    if n:
                        out.setdefault(norm(n), g.id)
        out.pop("", None)
        return out

    def _client(self) -> InfluxClient:
        return InfluxClient(self.url, self.database, secret(self.user_ref), secret(self.password_ref), self.timeout)

    # -- the loop ------------------------------------------------------------------------
    async def run(self) -> None:
        while True:
            started, wait = time.monotonic(), 0.0
            try:
                await self.once()
            except InfluxError as e:
                self.error, wait = str(e), BACKOFF_S
                forget_database(self.url)
            except Exception as e:  # noqa: BLE001 - a surprise in the data must not stop the explorer
                log.exception("Mobius explorer failed")
                self.error, wait = f"explorer error ({type(e).__name__})", BACKOFF_S
            await asyncio.sleep(max(wait, PASS_S - (time.monotonic() - started), 5.0))

    async def once(self, now: float | None = None) -> None:
        """One pass over every measurement, merged into what is kept."""
        now = time.time() if now is None else now
        t0 = time.monotonic()
        window = FIRST_WINDOW if not self.passes else WINDOW
        async with self._client() as c:
            if now - self.schema_at >= SCHEMA_EVERY_S:
                self.schema = await self._layout(c)
                self.schema_at = now
            for i, ((db, m), s) in enumerate(list(self.schema.items())):
                if i and self.pause:
                    await asyncio.sleep(self.pause)
                series = await c.query(f"SELECT * FROM {q_ident(m)} WHERE time > now() - {window} "
                                       f"GROUP BY * ORDER BY time DESC LIMIT 3", db)
                for ser in series:
                    self._merge(db, m, s, ser)
        self._expire(now)
        self.passes += 1
        self.pass_s = round(time.monotonic() - t0, 1)
        self.error, self.last_ok = None, now

    def _merge(self, db: str, m: str, s: dict, ser: dict) -> None:
        tags = ser.get("tags") or {}
        who = s["who"]
        value = tags.get(who) if who else None
        if who and value in (None, ""):                         # a point without the tag: site-wide
            who = None
        others = {k: str(v) for k, v in sorted(tags.items()) if k != who and v}
        rest = ", ".join(f"{k}={v}" for k, v in others.items())
        rows, pos = self._rows(m, rest, ser)
        if not rows:
            return
        if not who:
            for r in rows:
                old = self.site_rows.get((r["m"], r["f"]))
                if not old or r["t"] >= old["t"]:
                    self.site_rows[(r["m"], r["f"])] = r
            return
        gid = self.names.get(norm(value))
        if gid:
            kept = self.kept.setdefault(gid, {})
            for r in rows:
                r["_q"] = {"db": db, "who": who, "value": str(value), "tags": others}
                old = kept.get((r["m"], r["f"]))
                if not old or r["t"] >= old["t"]:
                    kept[(r["m"], r["f"])] = r
            if pos and (gid not in self.positions or pos[2] > self.positions[gid][2]):
                self.positions[gid] = pos
        else:
            u = self.unmatched.setdefault(f"{who}={value}", {"tag": who, "value": str(value)[:80], "t": 0.0,
                                                             "measurements": [], "fields": 0, "_keys": set()})
            u["t"] = max(u["t"], max(r["t"] for r in rows))
            u["_keys"].update((r["m"], r["f"]) for r in rows)
            u["fields"] = len(u["_keys"])
            if m not in u["measurements"]:
                u["measurements"].append(m)

    def _expire(self, now: float) -> None:
        """Values not seen for KEEP_S go; a group keeps its newest MAX_ROWS."""
        for gid in list(self.kept):
            kept = {k: r for k, r in self.kept[gid].items() if now - r["t"] <= KEEP_S}
            if len(kept) > MAX_ROWS:
                kept = dict(sorted(kept.items(), key=lambda kv: -kv[1]["t"])[:MAX_ROWS])
            if kept:
                self.kept[gid] = kept
            else:
                del self.kept[gid]
        self.positions = {g: p for g, p in self.positions.items() if now - p[2] <= KEEP_S}
        self.unmatched = {k: u for k, u in self.unmatched.items() if now - u["t"] <= KEEP_S}
        site = {k: r for k, r in self.site_rows.items() if now - r["t"] <= KEEP_S}
        self.site_rows = dict(sorted(site.items(), key=lambda kv: -kv[1]["t"])[:MAX_ROWS])

    async def _layout(self, c: InfluxClient) -> dict[tuple[str, str], dict]:
        await c.ping()                                              # its version: 3.x gets time limits
        out: dict[tuple[str, str], dict] = {}
        for db in [await find_database(c, self.mapped)]:            # the same database the readers use
            ms = [v[0] for s in await c.query("SHOW MEASUREMENTS", db) for v in s.get("values") or []]
            ms = [m for m in ms if m in self.mapped] + [m for m in ms if m not in self.mapped]     # the mapped first
            for m in ms[:MAX_MEASUREMENTS]:
                fields = [v[0] for s in await c.query(f"SHOW FIELD KEYS FROM {q_ident(m)}", db) for v in s.get("values") or []]
                tags = [v[0] for s in await c.query(f"SHOW TAG KEYS FROM {q_ident(m)}", db) for v in s.get("values") or []]
                out[(db, m)] = {"fields": [f for f in fields if not SECRET_LIKE.search(f)], "tags": tags,
                                "who": await self._who(c, db, m, tags)}
        return out

    async def _who(self, c: InfluxClient, db: str, m: str, tags: list[str]) -> str | None:
        if self.asset_tag and self.asset_tag in tags:
            return self.asset_tag
        best, hits = None, 0
        where = f" WHERE time > now() - {TAG_WINDOW}" if c.v3 else ""     # InfluxDB 3: never over all time
        tags = [k for k in tags if not PEOPLE_LIKE.search(k)]               # a user name is not a vehicle
        for k in tags[:12]:
            vals = [v[1] for s in await c.query(f"SHOW TAG VALUES FROM {q_ident(m)} WITH KEY = {q_ident(k)}{where} LIMIT 500", db)
                    for v in s.get("values") or [] if len(v) > 1]
            n = sum(1 for v in vals if norm(v) in self.names)
            if n > hits:
                best, hits = k, n
        return best or next((k for k in tags if ASSET_LIKE.search(k)), None)

    @staticmethod
    def _rows(m: str, rest: str, ser: dict) -> tuple[list[dict], tuple[float, float, float] | None]:
        """The newest non-empty value of each field in one series."""
        cols = ser.get("columns") or []
        newest: dict[str, tuple[object, float]] = {}
        for row in ser.get("values") or []:
            rec = dict(zip(cols, row))
            t = rec.get("time")
            if not isinstance(t, (int, float)):
                continue
            for f, v in rec.items():
                if f == "time" or v is None or f in newest or SECRET_LIKE.search(f):
                    continue
                newest[f] = (v, float(t))
        rows = []
        for f, (v, t) in newest.items():
            if isinstance(v, str):
                v = v[:MAX_TEXT]
            elif not isinstance(v, (bool, int, float)):
                v = str(v)[:MAX_TEXT]
            rows.append({"m": m, "f": f + (f" [{rest}]" if rest else ""), "v": v, "t": t, "_field": f})
        lat = next((newest[f] for f in newest if LAT.match(f)), None)
        lon = next((newest[f] for f in newest if LON.match(f)), None)
        pos = None
        if lat and lon and all(isinstance(x[0], (int, float)) and not isinstance(x[0], bool) for x in (lat, lon)):
            if -90 <= lat[0] <= 90 and -180 <= lon[0] <= 180 and (lat[0], lon[0]) != (0, 0):
                pos = (float(lat[0]), float(lon[0]), min(lat[1], lon[1]))
        return rows, pos

    # -- one field's history, for its chart (0.11.0) ----------------------------------------
    async def history(self, gid: str, m: str, f: str, span: str, now: float | None = None) -> dict:
        """The mean of a value the explorer has seen for the group, over `span`, in at most MAX_POINTS
        buckets. KeyError: no such value; ValueError: not a number, or an unknown span. Nothing the
        caller typed reaches the query - the measurement, field and tags come from what was read."""
        if span not in SPANS:
            raise ValueError(f"span: one of {', '.join(SPANS)}")
        row = (self.kept.get(gid) or {}).get((m, f))
        if row is None:
            raise KeyError((m, f))
        if isinstance(row["v"], bool) or not isinstance(row["v"], (int, float)):
            raise ValueError("only a number has a chart")
        now = time.time() if now is None else now
        key = (gid, m, f, span)
        hit = self._history.get(key)
        if hit and now - hit[0] < HISTORY_CACHE_S:
            return hit[1]
        q = row["_q"]
        where = " AND ".join([f"{q_ident(q['who'])} = {q_str(q['value'])}"]
                             + [f"{q_ident(k)} = {q_str(v)}" for k, v in q["tags"].items()])
        sql = (f"SELECT mean({q_ident(row['_field'])}) FROM {q_ident(m)} WHERE {where} AND time > now() - {span} "
               f"GROUP BY time({SPANS[span]}) fill(none)")
        async with self._client() as c:
            series = await c.query(sql, q["db"])
        points = [[t, v] for ser in series for t, v in (ser.get("values") or [])
                  if isinstance(t, (int, float)) and isinstance(v, (int, float)) and not isinstance(v, bool)]
        out = {"m": m, "f": f, "span": span, "points": sorted(points)[-MAX_POINTS:]}
        self._history[key] = (now, out)
        if len(self._history) > 200:
            self._history = {k: v for k, v in self._history.items() if now - v[0] < HISTORY_CACHE_S}
        return out

    # -- what the web pages and the report get ------------------------------------------------
    @property
    def groups(self) -> dict[str, dict]:
        """gid -> {"t": newest, "rows": [...]} (the kept values, by measurement and field)."""
        out = {}
        for gid, kept in self.kept.items():
            rows = sorted((_public(r) for r in kept.values()), key=lambda r: (r["m"], r["f"]))
            out[gid] = {"t": max(r["t"] for r in rows), "rows": rows}
        return out

    @property
    def site(self) -> list[dict]:
        return sorted((_public(r) for r in self.site_rows.values()), key=lambda r: (r["m"], r["f"]))

    def summary(self) -> dict:
        return {"enabled": True, "ok": self.error is None and self.last_ok is not None, "error": self.error,
                "last": self.last_ok, "groups": {gid: max(r["t"] for r in kept.values()) for gid, kept in self.kept.items()},
                "positions": {gid: {"lat": p[0], "lon": p[1], "t": p[2]} for gid, p in self.positions.items()},
                "unmatched": len(self.unmatched)}

    def group(self, gid: str) -> dict:
        kept = self.kept.get(gid) or {}
        rows = sorted((_public(r) for r in kept.values()), key=lambda r: (r["m"], r["f"]))
        return {"enabled": True, "error": self.error, "last": self.last_ok, "t": max((r["t"] for r in rows), default=None),
                "rows": rows, "measurements": len({r["m"] for r in rows}), "tables_read": len(self.schema),
                "pass_s": self.pass_s, "every_s": PASS_S, "keep_s": KEEP_S}

    def everything(self) -> dict:
        return {"enabled": True, "error": self.error, "last": self.last_ok,
                "measurements": len(self.schema),
                "groups": {gid: {"t": max(r["t"] for r in kept.values()), "fields": len(kept)} for gid, kept in self.kept.items()},
                "unmatched": sorted((_public(u) for u in self.unmatched.values()), key=lambda u: -u["t"]),
                "site": self.site}

    def report(self) -> dict:
        """For the problem report: counts only - no names of what Mobius sends, no values."""
        return {"error": self.error, "last_ok": self.last_ok, "measurements": len(self.schema),
                "groups_matched": len(self.kept), "values": sum(len(k) for k in self.kept.values()),
                "not_in_inventory": len(self.unmatched), "site_wide_values": len(self.site_rows),
                "pass_s": self.pass_s}
