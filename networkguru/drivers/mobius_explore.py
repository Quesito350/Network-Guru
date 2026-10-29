"""Everything the Mobius InfluxDB has, shown on the vehicle / location / client pages (0.8.0).

A side reader, not a component poller: it never changes a status. In live mode, when the site has a
Mobius InfluxDB URL:

    every 30 min   the layout - measurements, field keys, tag keys - and, per measurement, the "who"
                   tag: the configured asset_tag, else the tag whose values match inventory names
                   (Mobius asset, group name, device names, Mobius machine ID), else an asset-like tag
    every 60 s     the newest value of every field of the last 10 minutes, per series
                   (`SELECT * ... GROUP BY * ORDER BY time DESC LIMIT 3`), sorted onto the groups

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
from .influx import ASSET_LIKE, PEOPLE_LIKE, InfluxClient, InfluxError, find_database, forget_database, q_ident
from .mobius_map import map_tables, mobius_map

log = logging.getLogger("networkguru.drivers")

EVERY_S = 60.0
SCHEMA_EVERY_S = 1800.0
BACKOFF_S = 300.0
WINDOW = "10m"
TAG_WINDOW = "1d"
MAX_MEASUREMENTS = 60          # the mapped ones first (mobius_map), then the rest by name
MAX_ROWS = 300                 # values kept per group
MAX_TEXT = 200
SECRET_LIKE = re.compile(r"pass|secret|token|key|credential|auth", re.IGNORECASE)
LAT = re.compile(r"^(lat|latitude)$", re.IGNORECASE)
LON = re.compile(r"^(lon|lng|long|longitude)$", re.IGNORECASE)


def norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


class MobiusExplorer:
    def __init__(self, inv, timeout: float = 10.0):
        cfg = inv.data.mobius
        ex = cfg.model_extra or {}
        self.inv = inv
        self.url, self.database, self.asset_tag = cfg.influx_url, cfg.database, cfg.asset_tag
        self.user_ref, self.password_ref = ex.get("user_ref") or "", ex.get("password_ref") or ""
        self.timeout = timeout
        self.names = self._names(inv)
        self.mapped = map_tables(mobius_map(inv))
        self.schema: dict[tuple[str, str], dict] = {}          # (db, measurement) -> {"fields", "tags", "who"}
        self.schema_at = -1e18
        self.groups: dict[str, dict] = {}                      # gid -> {"t": newest, "rows": [...]}
        self.positions: dict[str, tuple[float, float, float]] = {}
        self.unmatched: dict[str, dict] = {}
        self.site: list[dict] = []
        self.error: str | None = None
        self.last_ok: float | None = None

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
            wait = EVERY_S
            try:
                await self.once()
            except InfluxError as e:
                self.error, wait = str(e), BACKOFF_S
                forget_database(self.url)
            except Exception as e:  # noqa: BLE001 - a surprise in the data must not stop the explorer
                log.exception("Mobius explorer failed")
                self.error, wait = f"explorer error ({type(e).__name__})", BACKOFF_S
            await asyncio.sleep(wait)

    async def once(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        async with self._client() as c:
            if now - self.schema_at >= SCHEMA_EVERY_S:
                self.schema = await self._layout(c)
                self.schema_at = now
            groups: dict[str, dict] = {}
            unmatched: dict[str, dict] = {}
            site: list[dict] = []
            positions: dict[str, tuple[float, float, float]] = {}
            for (db, m), s in self.schema.items():
                series = await c.query(f"SELECT * FROM {q_ident(m)} WHERE time > now() - {WINDOW} "
                                       f"GROUP BY * ORDER BY time DESC LIMIT 3", db)
                for ser in series:
                    tags = ser.get("tags") or {}
                    who = s["who"]
                    value = tags.get(who) if who else None
                    if who and value in (None, ""):                         # a point without the tag: site-wide
                        who = None
                    rest = ", ".join(f"{k}={v}" for k, v in sorted(tags.items()) if k != who and v)
                    rows, pos = self._rows(m, rest, ser)
                    if not rows:
                        continue
                    if not who:
                        site.extend(rows)
                        continue
                    gid = self.names.get(norm(value))
                    if gid:
                        g = groups.setdefault(gid, {"t": 0.0, "rows": []})
                        g["rows"].extend(rows)
                        g["t"] = max(g["t"], max(r["t"] for r in rows))
                        if pos and (gid not in positions or pos[2] > positions[gid][2]):
                            positions[gid] = pos
                    else:
                        u = unmatched.setdefault(f"{who}={value}", {"tag": who, "value": str(value)[:80], "t": 0.0,
                                                                    "measurements": [], "fields": 0})
                        u["t"] = max(u["t"], max(r["t"] for r in rows))
                        u["fields"] += len(rows)
                        if m not in u["measurements"]:
                            u["measurements"].append(m)
        for g in groups.values():
            g["rows"] = sorted(g["rows"], key=lambda r: -r["t"])[:MAX_ROWS]
            g["rows"].sort(key=lambda r: (r["m"], r["f"]))
        self.groups, self.unmatched, self.positions = groups, unmatched, positions
        self.site = sorted(site, key=lambda r: (r["m"], r["f"]))[:MAX_ROWS]
        self.error, self.last_ok = None, now

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
            rows.append({"m": m, "f": f + (f" [{rest}]" if rest else ""), "v": v, "t": t})
        lat = next((newest[f] for f in newest if LAT.match(f)), None)
        lon = next((newest[f] for f in newest if LON.match(f)), None)
        pos = None
        if lat and lon and all(isinstance(x[0], (int, float)) and not isinstance(x[0], bool) for x in (lat, lon)):
            if -90 <= lat[0] <= 90 and -180 <= lon[0] <= 180 and (lat[0], lon[0]) != (0, 0):
                pos = (float(lat[0]), float(lon[0]), min(lat[1], lon[1]))
        return rows, pos

    # -- what the web pages and the report get ------------------------------------------------
    def summary(self) -> dict:
        return {"enabled": True, "ok": self.error is None and self.last_ok is not None, "error": self.error,
                "last": self.last_ok, "groups": {gid: g["t"] for gid, g in self.groups.items()},
                "positions": {gid: {"lat": p[0], "lon": p[1], "t": p[2]} for gid, p in self.positions.items()},
                "unmatched": len(self.unmatched)}

    def group(self, gid: str) -> dict:
        g = self.groups.get(gid) or {"t": None, "rows": []}
        return {"enabled": True, "error": self.error, "last": self.last_ok, "t": g["t"], "rows": g["rows"]}

    def everything(self) -> dict:
        return {"enabled": True, "error": self.error, "last": self.last_ok,
                "measurements": len(self.schema),
                "groups": {gid: {"t": g["t"], "fields": len(g["rows"])} for gid, g in self.groups.items()},
                "unmatched": sorted(self.unmatched.values(), key=lambda u: -u["t"]),
                "site": self.site}

    def report(self) -> dict:
        """For the problem report: counts only - no names of what Mobius sends, no values."""
        return {"error": self.error, "last_ok": self.last_ok, "measurements": len(self.schema),
                "groups_matched": len(self.groups), "values": sum(len(g["rows"]) for g in self.groups.values()),
                "not_in_inventory": len(self.unmatched), "site_wide_values": len(self.site)}
