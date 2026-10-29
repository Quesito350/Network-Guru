"""The Mobius reader: VCU, GNSS, VAI (and other Mobius-sourced) values from the Mobius InfluxDB.

One poller for every mapped component. Mobius keeps one table per value and writes many of them only
when they change, so the reader keeps each vehicle's last value: per mapped table it asks for the
newest point of each vehicle (`... WHERE time > now() - 2m GROUP BY "<vehicle tag>" ORDER BY time DESC
LIMIT 1`); the first ask of a table - at start and after an outage - looks back FIRST_WINDOW. Every
query has a time limit (InfluxDB 3 refuses, or works hard on, queries over all time).

A vehicle is live while its newest link value (vcu_communicating) is true and something of it arrived
in the last QUIET_LINK_S - or, without a link value, in the last STALE_S. A live vehicle's components
get their last values (text
through the map's `values`, true / false as 1 / 0; a GNSS lat / lon becomes info for the map); a
vehicle that is not communicating gets Offline with the reason, its old values not judged.

The database is the configured one, else the one holding the map's tables (influx.find_database). Every
vehicle's position and link go to VehiclePositions (0.9.3, the map) - the vehicles not in the inventory
too; the position table's first ask looks back LONG_WINDOW, so parked vehicles have a place on the
first start (FIRST_WINDOW when InfluxDB refuses that).
"""

from __future__ import annotations

import re
import time

from ..config import secret
from ..model import CompResult, Metric
from ..positions import VehiclePositions
from .influx import InfluxClient, InfluxError, find_database, forget_database, q_ident
from .mobius_map import LINK, as_number, map_tables, mobius_map

STALE_S = 60.0
QUIET_LINK_S = 600.0        # link true, but nothing at all from the vehicle for this long: quiet after all
WINDOW = "2m"
FIRST_WINDOW = "24h"
LONG_WINDOW = "7d"          # the position table's first ask (parked vehicles) ...
LONG_TIMEOUT_S = 8.0        # ... given this long: the scheduler waits for the whole poll
INFO_KEYS = ("lat", "lon")
ALWAYS = {("vcu", LINK), ("gnss", "lat"), ("gnss", "lon")}     # read even without such a component (the map)


def norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def ago(seconds: float) -> str:
    s = max(0.0, seconds)
    return (f"{s:.0f} s" if s < 120 else f"{s / 60:.0f} min" if s < 7200 else f"{s / 3600:.0f} h" if s < 172800
            else f"{s / 86400:.0f} days")


class MobiusPoller:
    def __init__(self, inv, comps: list, interval: float = 15.0, positions: VehiclePositions | None = None):
        self.inv = inv
        self.positions = positions or VehiclePositions()
        self.map = mobius_map(inv)
        self.comps = comps
        self.comp_ids = [c.id for c in comps]
        self.interval = interval
        self.timeout = 30.0
        cfg = inv.data.mobius
        self.url, self.database = cfg.influx_url, cfg.database
        self.default_tag = cfg.asset_tag or "asset"
        self.extra = cfg.model_extra or {}
        self.type_of = {c.id: c.type for c in comps}
        self.group_of = {c.id: inv.comp_group[c.id] for c in comps}
        self.by_group: dict[tuple[str, str], str] = {}           # (group id, type) -> component id
        self.exact: dict[str, str] = {}                           # Mobius asset name -> group id
        self.loose: dict[str, str] = {}                           # the same, normalised
        for c in comps:
            gid = self.group_of[c.id]
            self.by_group.setdefault((gid, c.type), c.id)
            g = inv.groups[gid]
            for n in (g.mobius_asset, g.name):
                if n:
                    self.exact.setdefault(str(n), gid)
                    self.loose.setdefault(norm(n), gid)
        for g in inv.groups.values():                              # the map matches every vehicle
            if inv.group_tab.get(g.id) == "vehicles":
                for n in (g.mobius_asset, g.name):
                    if n:
                        self.exact.setdefault(str(n), g.id)
                        self.loose.setdefault(norm(n), g.id)
        # (table, vehicle tag) -> {field: [(type, our name, spec)]}; the link and the position are read
        # even without a VCU / GNSS component
        types = set(self.type_of.values())
        self.tables: dict[tuple[str, str], dict[str, list]] = {}
        self.long_first: set[tuple[str, str]] = set()
        for typ, values in self.map.items():
            for ours, spec in values.items():
                if typ not in types and (typ, ours) not in ALWAYS:
                    continue
                key = (spec["measurement"], spec.get("asset_tag") or self.default_tag)
                self.tables.setdefault(key, {}).setdefault(spec["field"], []).append((typ, ours, spec))
                if typ == "gnss" and ours in INFO_KEYS:
                    self.long_first.add(key)
        self.table_names = map_tables(self.map)
        self.coords: dict[str, dict[str, tuple[float, float]]] = {}     # vehicle name -> {lat / lon: (time, value)}
        self.seeded: set[tuple[str, str]] = set()
        self.last: dict[tuple[str, str], tuple[float, float]] = {}     # (group id, our name) -> (time, value)
        self.heard: dict[str, float] = {}                              # group id -> newest point of any table

    def _when(self, window: str) -> str:
        return f"time > now() - {window}"

    def _now(self) -> float:
        return time.time()

    def _group(self, asset: str) -> str | None:
        return self.exact.get(asset) or self.loose.get(norm(asset))

    def vehicle_of(self, asset: str) -> str | None:
        """The inventory vehicle a Mobius name belongs to, or None (the map's grey pins)."""
        gid = self._group(asset)
        return gid if gid and self.inv.group_tab.get(gid) == "vehicles" else None

    async def poll(self, now: float) -> list[CompResult]:
        try:
            async with InfluxClient(self.url, self.database, secret(self.extra.get("user_ref") or ""),
                                    secret(self.extra.get("password_ref") or ""), self.timeout) as c:
                await find_database(c, self.table_names)
                for key, fields in self.tables.items():
                    table, tag = key
                    sel = f"SELECT {', '.join(q_ident(f) for f in fields)} FROM {q_ident(table)} WHERE "
                    end = f" GROUP BY {q_ident(tag)} ORDER BY time DESC LIMIT 1"
                    if key in self.seeded:
                        rows = await c.query(sel + self._when(WINDOW) + end)
                    elif key in self.long_first:
                        try:
                            rows = await c.query(sel + self._when(LONG_WINDOW) + end, timeout=LONG_TIMEOUT_S)
                        except InfluxError:                 # InfluxDB 3: too many files, or slow - one day then
                            rows = await c.query(sel + self._when(FIRST_WINDOW) + end)
                    else:
                        rows = await c.query(sel + self._when(FIRST_WINDOW) + end)
                    self.seeded.add(key)
                    for s in rows:
                        self._take(tag, s, fields)
        except InfluxError as e:
            self.seeded.clear()                     # after the outage, look back again
            forget_database(self.url)
            return [CompResult(cid, now, False, error=f"Mobius InfluxDB: {e}") for cid in self.comp_ids]
        finally:
            self.positions.save()
        return [self._result(cid, now) for cid in self.comp_ids]

    def _take(self, tag: str, s: dict, fields: dict[str, list]) -> None:
        name = str((s.get("tags") or {}).get(tag, ""))
        if not name or not s.get("values"):
            return
        row = dict(zip(s.get("columns") or [], s["values"][0]))
        t = row.get("time")
        if not isinstance(t, (int, float)):
            return
        t = float(t)
        self._fleet(name, row, t, fields)
        gid = self._group(name)
        if not gid:
            return
        self.heard[gid] = max(self.heard.get(gid, 0.0), t)
        for f, uses in fields.items():
            for _typ, ours, spec in uses:
                v = as_number(spec, row.get(f))
                if v is None:
                    continue
                old = self.last.get((gid, ours))
                if old is None or t >= old[0]:
                    self.last[(gid, ours)] = (t, v)

    def _fleet(self, name: str, row: dict, t: float, fields: dict[str, list]) -> None:
        """Every vehicle's position and link, for the map - also when it is not in the inventory."""
        self.positions.heard(name, t)
        for f, uses in fields.items():
            for typ, ours, spec in uses:
                v = row.get(f)
                if ours == LINK:
                    n = as_number(spec, v)
                    if n is not None:
                        self.positions.link(name, n >= 0.5, t)
                elif typ == "gnss" and ours in INFO_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool):
                    xy = self.coords.setdefault(name, {})
                    if t >= xy.get(ours, (0.0, 0.0))[0]:
                        xy[ours] = (t, float(v))
                    if "lat" in xy and "lon" in xy:      # one table, or two (one per value)
                        self.positions.position(name, xy["lat"][1], xy["lon"][1], min(xy["lat"][0], xy["lon"][0]))

    def _result(self, cid: str, now: float) -> CompResult:
        gid, typ = self.group_of[cid], self.type_of[cid]
        wall = self._now()
        link = self.last.get((gid, LINK))
        heard = self.heard.get(gid)
        if link is not None and link[1] < 0.5:
            return CompResult(cid, now, False, error=f"not communicating with Mobius for {ago(wall - link[0])}")
        if heard is None:
            return CompResult(cid, now, False, error="no Mobius data in the last day (vehicle off, or its Mobius asset "
                                                    "name does not match the name in Mobius)")
        if wall - heard > (QUIET_LINK_S if link is not None else STALE_S):
            return CompResult(cid, now, False, error=f"no Mobius data for {ago(wall - heard)}"
                                                    + (" (Mobius last said it was communicating)" if link else ""))
        ours = self.map.get(typ, {})
        metrics, info = [], {}
        for name in ours:
            got = self.last.get((gid, name))
            if got is None:
                continue
            if name in INFO_KEYS:
                info[name] = got[1]
            else:
                metrics.append(Metric(name, got[1]))
        return CompResult(cid, now, True, metrics + [Metric("read_ok", 1.0, {"via": "mobius"})], info)
