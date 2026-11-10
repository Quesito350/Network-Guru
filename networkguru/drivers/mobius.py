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

While someone has the Map open (0.9.5: `watch()`, from /api/map/vehicles), the runtime also calls
`poll_positions()` every few seconds: only the position table(s), the last FAST_WINDOW, so a vehicle moves
on the map as it drives. It never gives a status - poll() does, every `interval`.

0.9.7: what each vehicle is - the map's `vehicle` text values (IP, MAC, make, model, type, comms), newest per
vehicle tag, asked at most every IDENTITY_EVERY_S - goes to `identity`; mobius_vehicles.py turns the vehicles
not in the inventory into vehicles of their own. `extend()` takes on their components while running.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time

from ..config import secret
from ..model import CompResult, Metric
from ..positions import VehiclePositions
from .influx import InfluxClient, InfluxError, find_database, forget_database, q_ident
from .mobius_map import LINK, VEHICLE_INFO, as_number, map_tables, mobius_map

log = logging.getLogger("networkguru.drivers")

STALE_S = 60.0
QUIET_LINK_S = 600.0        # link true, but nothing at all from the vehicle for this long: quiet after all
WINDOW = "2m"
FIRST_WINDOW = "24h"
LONG_WINDOW = "7d"          # the position table's first ask (parked vehicles) ...
LONG_TIMEOUT_S = 8.0        # ... given this long: the scheduler waits for the whole poll
FAST_WINDOW = "30s"         # poll_positions(): the map's quick read
WATCH_S = 60.0              # ... while the Map was open this recently
INFO_KEYS = ("lat", "lon")
ALWAYS = {("vcu", LINK), ("gnss", "lat"), ("gnss", "lon")}     # read even without such a component (the map)
IDENTITY_EVERY_S = 600.0    # what a vehicle is changes seldom (0.9.7)
IDENTITY_WINDOW = "24h"
UNKNOWN = {"", "unknown", "none", "null", "n/a", "-"}
ZERO_MAC = re.compile(r"0{1,2}([-:.]0{1,2}){5}")


def norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def clean_text(v) -> str | None:
    """A Mobius text value as shown: trimmed, at most 80 characters, None for "unknown" and empties."""
    if v is None or isinstance(v, bool):
        return None
    t = str(v).strip()[:80]
    return None if t.lower() in UNKNOWN else t


def ipv4(v) -> str | None:
    try:
        ip = ipaddress.ip_address(str(v or "").strip())
    except ValueError:
        return None
    return str(ip) if ip.version == 4 and not ip.is_unspecified else None


def simulated(name: str, info: dict) -> bool:
    """A Mobius simulator vehicle: a SIM_ name, a loopback IP or an all-zero MAC (0.9.7)."""
    ip = info.get("ip") or ""
    return str(name).upper().startswith("SIM_") or ip.startswith("127.") or bool(info.get("zero_mac"))


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
        self._link(inv)
        # (table, vehicle tag) -> {field: [(type, our name, spec)]}; the link and the position are read
        # even without a VCU / GNSS component
        self.tables: dict[tuple[str, str], dict[str, list]] = {}
        self.long_first: set[tuple[str, str]] = set()
        self._build_tables()
        self.table_names = map_tables(self.map)
        # what each vehicle is (0.9.7): {name: {ip, mac, make, model, type, comms, zero_mac, t}}
        self.info_specs = {ours: spec for ours, spec in (self.map.get("vehicle") or {}).items() if ours in VEHICLE_INFO}
        self.identity: dict[str, dict] = {}
        self.identity_at = -1e18
        self.identity_seeded = False
        self.coords: dict[str, dict[str, tuple[float, float]]] = {}     # vehicle name -> {lat / lon: (time, value)}
        self.seeded: set[tuple[str, str]] = set()
        self.last: dict[tuple[str, str], tuple[float, float]] = {}     # (group id, our name) -> (time, value)
        self.heard: dict[str, float] = {}                              # group id -> newest point of any table
        self.watched_until = 0.0                                       # the Map is open (poll_positions)

    def _link(self, inv) -> None:
        """Mobius names -> groups: each component's group, and every vehicle (the map matches them all)."""
        self.exact, self.loose = {}, {}
        for c in self.comps:
            gid = self.group_of[c.id]
            self.by_group.setdefault((gid, c.type), c.id)
            g = inv.groups[gid]
            for n in (g.mobius_asset, g.name):
                if n:
                    self.exact.setdefault(str(n), gid)
                    self.loose.setdefault(norm(n), gid)
        for g in inv.groups.values():
            if inv.group_tab.get(g.id) == "vehicles":
                for n in (g.mobius_asset, g.name):
                    if n:
                        self.exact.setdefault(str(n), g.id)
                        self.loose.setdefault(norm(n), g.id)

    def _build_tables(self) -> None:
        types = set(self.type_of.values())
        tables: dict[tuple[str, str], dict[str, list]] = {}
        for typ, values in self.map.items():
            for ours, spec in values.items():
                if typ not in types and (typ, ours) not in ALWAYS:
                    continue
                key = (spec["measurement"], spec.get("asset_tag") or self.default_tag)
                tables.setdefault(key, {}).setdefault(spec["field"], []).append((typ, ours, spec))
                if typ == "gnss" and ours in INFO_KEYS:
                    self.long_first.add(key)
        self.tables = tables

    def extend(self, inv, comps: list) -> list[str]:
        """More Mobius components while running (a vehicle added from Mobius, 0.9.7): this reader reads them
        too - no second reader. Returns the new component ids."""
        self.inv = inv
        new = [c for c in comps if c.id not in self.type_of]
        for c in new:
            self.comps.append(c)
            self.comp_ids.append(c.id)
            self.type_of[c.id] = c.type
            self.group_of[c.id] = inv.comp_group[c.id]
        self._link(inv)
        self._build_tables()                # a type it had no component of yet: its tables, first ask a day back
        return [c.id for c in new]

    def simulated(self, name: str) -> bool:
        return simulated(name, self.identity.get(name) or {})

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

    def _client(self, timeout: float) -> InfluxClient:
        return InfluxClient(self.url, self.database, secret(self.extra.get("user_ref") or ""),
                            secret(self.extra.get("password_ref") or ""), timeout)

    def _newest(self, key: tuple[str, str], fields, window: str) -> str:
        """Each vehicle's newest point of one table in the last `window`."""
        table, tag = key
        return (f"SELECT {', '.join(q_ident(f) for f in fields)} FROM {q_ident(table)} WHERE {self._when(window)}"
                f" GROUP BY {q_ident(tag)} ORDER BY time DESC LIMIT 1")

    def watch(self, now: float | None = None) -> None:
        """The Map is open: read the positions often for a while (the runtime's position loop)."""
        self.watched_until = (self._now() if now is None else now) + WATCH_S

    def watched(self, now: float | None = None) -> bool:
        return (self._now() if now is None else now) < self.watched_until

    async def poll_positions(self) -> int:
        """The map's quick read: the position table(s) only, once poll() has looked back over them. Never a
        status. Returns how many vehicles it heard from."""
        keys = [k for k in self.long_first if k in self.seeded]
        if not keys:
            return 0
        heard = 0
        try:
            async with self._client(self.timeout) as c:
                await find_database(c, self.table_names)
                for key in keys:
                    fields = self.tables[key]
                    for s in await c.query(self._newest(key, fields, FAST_WINDOW)):
                        self._take(key[1], s, fields)
                        heard += 1
        finally:
            self.positions.save()
        return heard

    async def poll(self, now: float) -> list[CompResult]:
        try:
            async with self._client(self.timeout) as c:
                await find_database(c, self.table_names)
                for key, fields in self.tables.items():
                    tag = key[1]
                    if key in self.seeded:
                        rows = await c.query(self._newest(key, fields, WINDOW))
                    elif key in self.long_first:
                        try:
                            rows = await c.query(self._newest(key, fields, LONG_WINDOW), timeout=LONG_TIMEOUT_S)
                        except InfluxError:                 # InfluxDB 3: too many files, or slow - one day then
                            rows = await c.query(self._newest(key, fields, FIRST_WINDOW))
                    else:
                        rows = await c.query(self._newest(key, fields, FIRST_WINDOW))
                    self.seeded.add(key)
                    for s in rows:
                        self._take(tag, s, fields)
                if self.info_specs and self._now() - self.identity_at >= IDENTITY_EVERY_S:
                    await self._identity(c)
        except InfluxError as e:
            self.seeded.clear()                     # after the outage, look back again
            forget_database(self.url)
            return [CompResult(cid, now, False, error=f"Mobius InfluxDB: {e}") for cid in self.comp_ids]
        finally:
            self.positions.save()
        return [self._result(cid, now) for cid in self.comp_ids]

    async def _identity(self, c: InfluxClient) -> None:
        """What each vehicle is, from the map's `vehicle` text values (0.9.7). A refused or slow ask is tried
        again later - never a reason to fail the poll."""
        by_table: dict[tuple[str, str], dict[str, str]] = {}
        for ours, spec in self.info_specs.items():
            key = (spec["measurement"], spec.get("asset_tag") or self.default_tag)
            by_table.setdefault(key, {})[ours] = spec["field"]
        window = LONG_WINDOW if not self.identity_seeded else IDENTITY_WINDOW
        try:
            for (table, tag), wanted in by_table.items():
                fields = sorted(set(wanted.values()))
                q = (f"SELECT {', '.join(q_ident(f) for f in fields)} FROM {q_ident(table)} WHERE {self._when(window)}"
                     f" GROUP BY {q_ident(tag)} ORDER BY time DESC LIMIT 1")
                try:
                    rows = await c.query(q, timeout=LONG_TIMEOUT_S)
                except InfluxError:
                    if window == IDENTITY_WINDOW:
                        raise
                    rows = await c.query(q.replace(self._when(window), self._when(IDENTITY_WINDOW)))
                for ser in rows:
                    name = str((ser.get("tags") or {}).get(tag, ""))
                    if not name or not ser.get("values"):
                        continue
                    row = dict(zip(ser.get("columns") or [], ser["values"][0]))
                    e = self.identity.setdefault(name, {})
                    if isinstance(row.get("time"), (int, float)):
                        e["t"] = max(float(row["time"]), e.get("t", 0.0))
                    for ours, field in wanted.items():
                        key = ours[len("vehicle_"):]
                        raw = row.get(field)
                        if key == "mac" and raw is not None and ZERO_MAC.fullmatch(str(raw).strip()):
                            e["zero_mac"] = True
                            continue
                        v = ipv4(raw) if key == "ip" else clean_text(raw)
                        if v:
                            e[key] = v
            self.identity_seeded = True
        except InfluxError as e:
            log.warning("Mobius vehicle identity not read (%s) - trying again in %.0f min", e, IDENTITY_EVERY_S / 60)
        self.identity_at = self._now()

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
