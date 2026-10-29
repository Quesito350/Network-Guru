"""Device readers: the part of a live poll that asks the device itself for its values.

    rajant, tachyon, switch   SNMP (v3 for Rajant, v2c community for Tachyon and the switches)
    cell_modem                by model (cellular.py): SNMP for Sierra ALEOS / MikroTik / OpenWrt,
                              ALMS (alms.py) for the Sierra XR80 / XR90
    starlink                  the dish's status API through the router's NAT (starlink.py)
    netmaker                  the gateway's node counts (netmaker.py)
    victron_cerbo             Modbus TCP (Victron register list)
    influxdb                  the newest write time in the Mobius InfluxDB (influx_write_age_s)
    mobius_server             its communicating vehicles and connected clients, from the Mobius InfluxDB

Logins are named on the component (`*_ref`, from the inventory's per-type defaults) and read from
secrets.env on every read, so adding them later needs no restart. A reader whose secrets are
missing is "not set up": it is skipped, the device is still pinged.
"""

from __future__ import annotations

import time

from ..config import secret
from ..model import Metric
from . import cellular, snmp_profiles, victron
from .influx import InfluxClient, InfluxError, find_database, forget_database, q_ident
from .mobius_map import as_number, map_tables, mobius_map
from .modbus import ModbusClient, ModbusError
from .snmp import SnmpClient, SnmpError

SNMP_TYPES = ("rajant", "tachyon", "switch")
READ_EVERY_S = 30.0
AGE_LONG_EVERY_S = 600.0
LINK_RSSI: dict[str, dict[str, tuple[float, float]]] = {}    # Tachyon link -> {comp id: (time, rssi)}


class ReadError(Exception):
    pass


class SnmpReader:
    via = "snmp"

    def __init__(self, comp, profile: str | None = None):
        self.comp = comp
        self.profile = profile or comp.type                  # a cell modem: its kind (cellular.KINDS)
        ex = dict(comp.model_extra or {})
        if not any(k.startswith("snmp_") for k in ex):       # a hand-written inventory: the standard names
            from ..importers.build import CELL_DEFAULTS, TYPE_DEFAULTS
            ex.update(TYPE_DEFAULTS.get(comp.type) or (CELL_DEFAULTS["snmp"] if comp.type == "cell_modem" else {}))
        self.version = str(ex.get("snmp_version") or "2c")
        self.port = int(ex.get("snmp_port") or 161)
        self.refs = {k: ex[k] for k in ("snmp_community_ref", "snmp_user_ref", "snmp_auth_ref", "snmp_priv_ref")
                     if isinstance(ex.get(k), str) and ex[k]}
        self.state: dict = {}
        self._client: SnmpClient | None = None
        self._key = None

    @property
    def label(self) -> str:
        kind = cellular.KINDS.get(self.profile)
        return f"SNMP v{self.version}" + (f" ({kind})" if kind else "")

    def needed(self) -> list[str]:
        want = ("snmp_user_ref", "snmp_auth_ref", "snmp_priv_ref") if self.version == "3" else ("snmp_community_ref",)
        return [self.refs.get(k, f"({k} not set in the inventory)") for k in want]

    def missing(self) -> list[str]:
        return [n for n in self.needed() if n.startswith("(") or not secret(n)]

    def _client_for(self) -> SnmpClient:
        values = tuple(secret(self.refs[k]) if k in self.refs else None
                       for k in ("snmp_community_ref", "snmp_user_ref", "snmp_auth_ref", "snmp_priv_ref"))
        key = (self.comp.host, self.version, values)
        if self._client is None or key != self._key:          # logins changed: start again
            if self._client:
                self._client.close()
            community, user, auth, priv = values
            self._client = SnmpClient(self.comp.host.split(":")[0], self.port, version=self.version, community=community,
                                      user=user, auth_password=auth, priv_password=priv)
            self._key = key
        return self._client

    async def read(self, now: float) -> tuple[list[Metric], dict]:
        try:
            metrics, info = await snmp_profiles.read(self._client_for(), self.profile, self.state, now)
        except SnmpError as e:
            if self._client:                                   # a fresh discovery next time
                self._client.close()
                self._client = None
            hint = " - check the SNMP login and that UDP 161 is allowed from this server" if "no SNMP answer" in str(e) else ""
            raise ReadError(f"{e}{hint}") from None
        if self.comp.type == "tachyon" and self.comp.link:
            metrics.extend(self._remote(metrics, now))
        return metrics, info

    def _remote(self, metrics: list[Metric], now: float) -> list[Metric]:
        """The other end of the 60 GHz link hears us at this level (both ends are read)."""
        ends = LINK_RSSI.setdefault(self.comp.link, {})
        mine = next((m.value for m in metrics if m.name == "tachyon_rssi_dbm"), None)
        if mine is not None:
            ends[self.comp.id] = (now, mine)
        other = [v for cid, (t, v) in ends.items() if cid != self.comp.id and now - t < 120]
        return [Metric("tachyon_remote_rssi_dbm", other[0])] if other else []


class ModbusReader:
    via = "modbus"
    label = "Modbus TCP"

    def __init__(self, comp):
        self.comp = comp
        ex = comp.model_extra or {}
        self.port = int(ex.get("modbus_port") or 502)
        mu = ex.get("mppt_unit")
        self.mppt_unit = int(mu) if str(mu or "").strip().isdigit() else None

    def missing(self) -> list[str]:
        return []

    async def read(self, now: float) -> tuple[list[Metric], dict]:
        try:
            async with ModbusClient(self.comp.host.split(":")[0], self.port) as m:
                return await victron.read(m, self.mppt_unit)
        except ModbusError as e:
            raise ReadError(str(e)) from None


class _MobiusDb:
    """A reader of the Mobius InfluxDB (the login, if it has one, from secrets.env). Every query has a time limit."""
    via = "influx"
    label = "Mobius data"

    def __init__(self, comp, inv):
        self.comp, self.inv = comp, inv
        self.cfg = inv.data.mobius
        self.map = mobius_map(inv)
        self.tables = map_tables(self.map)
        self.tag = self.cfg.asset_tag or "asset"

    def missing(self) -> list[str]:
        ex = self.cfg.model_extra or {}
        return [ex[k] for k in ("user_ref", "password_ref") if isinstance(ex.get(k), str) and not secret(ex[k])]

    def client(self) -> InfluxClient:
        ex = self.cfg.model_extra or {}
        url = self.cfg.influx_url or f"http://{self.comp.host.split(':')[0]}:8086"
        return InfluxClient(url, self.cfg.database, secret(ex.get("user_ref") or ""), secret(ex.get("password_ref") or ""))

    async def pick(self, c: InfluxClient) -> None:
        """The database: the configured one, else the one holding the map's tables (find_database)."""
        await find_database(c, self.tables)

    def failed(self, c: InfluxClient | None, e: InfluxError) -> ReadError:
        forget_database(c.url if c else self.cfg.influx_url)
        return ReadError(str(e))


class InfluxAgeReader(_MobiusDb):
    """How old the newest Mobius write is: the map's influxdb / influx_write_age_s table (else the first
    mapped value) in the last hour; when the last hour has none, the last 7 days - asked at most every
    AGE_LONG_EVERY_S. Never over all time."""

    def __init__(self, comp, inv):
        super().__init__(comp, inv)
        self.spec = (self.map.get("influxdb") or {}).get("influx_write_age_s") or \
            next((s for values in self.map.values() for s in values.values()), None)
        self.newest: float | None = None
        self.long_asked = -1e18

    async def read(self, now: float) -> tuple[list[Metric], dict]:
        if not self.spec:
            return [], {}
        sel = f"SELECT {q_ident(self.spec['field'])} FROM {q_ident(self.spec['measurement'])}"
        c = None
        try:
            async with self.client() as c:
                await self.pick(c)
                t = await _newest(c, sel, "1h")
                if t is None and self.newest is None and now - self.long_asked >= AGE_LONG_EVERY_S:
                    self.long_asked = now
                    t = await _newest(c, sel, "7d")
        except InfluxError as e:
            raise self.failed(c, e) from None
        if t is not None:
            self.newest = max(self.newest or 0.0, t)
        if self.newest is None:
            raise ReadError("no Mobius data in the last 7 days")
        return [Metric("influx_write_age_s", max(0.0, time.time() - self.newest))], {}


async def _newest(c: InfluxClient, select: str, window: str) -> float | None:
    rows = await c.query(f"{select} WHERE time > now() - {window} ORDER BY time DESC LIMIT 1")
    t = rows[0]["values"][0][0] if rows and rows[0].get("values") else None
    return float(t) if isinstance(t, (int, float)) else None


class MobiusServerReader(_MobiusDb):
    """The Mobius server's counts from its InfluxDB (the map's mobius_server values):
    mobius_assets   vehicles whose newest link value of the last day is true
    mobius_clients  how many different values the clients table's field had in the last 5 minutes"""

    async def read(self, now: float) -> tuple[list[Metric], dict]:
        ours = self.map.get("mobius_server") or {}
        out = []
        c = None
        try:
            async with self.client() as c:
                await self.pick(c)
                spec = ours.get("mobius_assets")
                if spec:
                    tag, f = spec.get("asset_tag") or self.tag, spec["field"]
                    rows = await c.query(f"SELECT {q_ident(f)} FROM {q_ident(spec['measurement'])} WHERE time > now() - 24h "
                                         f"GROUP BY {q_ident(tag)} ORDER BY time DESC LIMIT 1")
                    live = sum(1 for s in rows if s.get("values")
                               and (as_number(spec, dict(zip(s.get("columns") or [], s["values"][0])).get(f)) or 0) >= 0.5)
                    out.append(Metric("mobius_assets", float(live)))
                spec = ours.get("mobius_clients")
                if spec:
                    rows = await c.query(f"SELECT {q_ident(spec['field'])} FROM {q_ident(spec['measurement'])} "
                                         "WHERE time > now() - 5m")
                    names = {str(v[1]) for s in rows for v in s.get("values") or [] if len(v) > 1 and v[1] not in (None, "")}
                    out.append(Metric("mobius_clients", float(len(names))))
        except InfluxError as e:
            raise self.failed(c, e) from None
        return out, {}


def cell_kind(comp) -> str | None:
    return cellular.kind(comp.model, (comp.model_extra or {}).get("reader"))


def reader_for(comp, inv):
    if comp.type == "cell_modem" and cell_kind(comp) == "alms":     # from Sierra's cloud: no address needed
        from .alms import AlmsReader
        return AlmsReader(comp)
    if comp.type == "starlink" and (comp.target or comp.host):
        from .starlink import StarlinkReader
        return StarlinkReader(comp)
    if not comp.host:
        return None
    if comp.type in SNMP_TYPES:
        return SnmpReader(comp)
    if comp.type == "cell_modem":
        kind = cell_kind(comp)
        return SnmpReader(comp, kind) if kind else None
    if comp.type == "netmaker":
        from .netmaker import NetmakerReader, api_for
        api = api_for(inv.data.netmaker)
        return NetmakerReader(comp, inv, api) if api else None
    if comp.type == "victron_cerbo":
        return ModbusReader(comp)
    if comp.type == "influxdb" and mobius_map(inv):
        return InfluxAgeReader(comp, inv)
    if comp.type == "mobius_server" and (mobius_map(inv).get("mobius_server")) and inv.data.mobius.influx_url:
        return MobiusServerReader(comp, inv)
    return None
