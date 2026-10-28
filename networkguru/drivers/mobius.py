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
"""

from __future__ import annotations

import re
import time

from ..config import secret
from ..model import CompResult, Metric
from .influx import InfluxClient, InfluxError, q_ident
from .mobius_map import LINK, as_number, mobius_map

STALE_S = 60.0
QUIET_LINK_S = 600.0        # link true, but nothing at all from the vehicle for this long: quiet after all
WINDOW = "2m"
FIRST_WINDOW = "24h"
INFO_KEYS = ("lat", "lon")


def norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def ago(seconds: float) -> str:
    s = max(0.0, seconds)
    return (f"{s:.0f} s" if s < 120 else f"{s / 60:.0f} min" if s < 7200 else f"{s / 3600:.0f} h" if s < 172800
            else f"{s / 86400:.0f} days")


class MobiusPoller:
    def __init__(self, inv, comps: list, interval: float = 15.0):
        self.inv = inv
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
        # (table, vehicle tag) -> {field: [(type, our name, spec)]}; the link is read even without a VCU
        types = set(self.type_of.values())
        self.tables: dict[tuple[str, str], dict[str, list]] = {}
        for typ, values in self.map.items():
            if typ not in types and not (typ == "vcu" and LINK in values):
                continue
            for ours, spec in values.items():
                if typ not in types and ours != LINK:
                    continue
                key = (spec["measurement"], spec.get("asset_tag") or self.default_tag)
                self.tables.setdefault(key, {}).setdefault(spec["field"], []).append((typ, ours, spec))
        self.seeded: set[tuple[str, str]] = set()
        self.last: dict[tuple[str, str], tuple[float, float]] = {}     # (group id, our name) -> (time, value)
        self.heard: dict[str, float] = {}                              # group id -> newest point of any table

    def _when(self, window: str) -> str:
        return f"time > now() - {window}"

    def _now(self) -> float:
        return time.time()

    def _group(self, asset: str) -> str | None:
        return self.exact.get(asset) or self.loose.get(norm(asset))

    async def poll(self, now: float) -> list[CompResult]:
        try:
            async with InfluxClient(self.url, self.database, secret(self.extra.get("user_ref") or ""),
                                    secret(self.extra.get("password_ref") or ""), self.timeout) as c:
                for key, fields in self.tables.items():
                    table, tag = key
                    window = WINDOW if key in self.seeded else FIRST_WINDOW
                    cols = ", ".join(q_ident(f) for f in fields)
                    rows = await c.query(f"SELECT {cols} FROM {q_ident(table)} WHERE {self._when(window)} "
                                         f"GROUP BY {q_ident(tag)} ORDER BY time DESC LIMIT 1")
                    self.seeded.add(key)
                    for s in rows:
                        self._take(tag, s, fields)
        except InfluxError as e:
            self.seeded.clear()                     # after the outage, look back again
            return [CompResult(cid, now, False, error=f"Mobius InfluxDB: {e}") for cid in self.comp_ids]
        return [self._result(cid, now) for cid in self.comp_ids]

    def _take(self, tag: str, s: dict, fields: dict[str, list]) -> None:
        gid = self._group(str((s.get("tags") or {}).get(tag, "")))
        if not gid or not s.get("values"):
            return
        row = dict(zip(s.get("columns") or [], s["values"][0]))
        t = row.get("time")
        if not isinstance(t, (int, float)):
            return
        t = float(t)
        self.heard[gid] = max(self.heard.get(gid, 0.0), t)
        for f, uses in fields.items():
            for _typ, ours, spec in uses:
                v = as_number(spec, row.get(f))
                if v is None:
                    continue
                old = self.last.get((gid, ours))
                if old is None or t >= old[0]:
                    self.last[(gid, ours)] = (t, v)

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
