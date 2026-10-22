"""Device readers: the part of a live poll that asks the device itself for its values.

    rajant, tachyon, switch   SNMP (v3 for Rajant, v2c community for Tachyon and the switches)
    cell_modem                by model (cellular.py): SNMP for Sierra ALEOS / MikroTik / OpenWrt,
                              ALMS (alms.py) for the Sierra XR80 / XR90
    starlink                  the dish's status API through the router's NAT (starlink.py)
    netmaker                  the gateway's node counts (netmaker.py)
    victron_cerbo             Modbus TCP (Victron register list)
    influxdb                  the newest write time in the Mobius InfluxDB (influx_write_age_s)

Logins are named on the component (`*_ref`, from the inventory's per-type defaults) and read from
secrets.env on every read, so adding them later needs no restart. A reader whose secrets are
missing is "not set up": it is skipped, the device is still pinged.
"""

from __future__ import annotations

import time

from ..config import secret
from ..model import Metric
from . import cellular, snmp_profiles, victron
from .influx import InfluxClient, InfluxError, q_ident
from .mobius_map import mobius_map
from .modbus import ModbusClient, ModbusError
from .snmp import SnmpClient, SnmpError

SNMP_TYPES = ("rajant", "tachyon", "switch")
READ_EVERY_S = 30.0
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


class InfluxAgeReader:
    """How old the newest Mobius write is - from the first mapped measurement."""
    via = "influx"
    label = "Mobius InfluxDB"

    def __init__(self, comp, inv):
        self.comp, self.inv = comp, inv
        self.cfg = inv.data.mobius

    def missing(self) -> list[str]:
        ex = self.cfg.model_extra or {}
        return [ex[k] for k in ("user_ref", "password_ref") if isinstance(ex.get(k), str) and not secret(ex[k])]

    async def read(self, now: float) -> tuple[list[Metric], dict]:
        spec = next(iter(mobius_map(self.inv).values()), None)
        if not spec:
            return [], {}
        ex = self.cfg.model_extra or {}
        url = self.cfg.influx_url or f"http://{self.comp.host.split(':')[0]}:8086"
        try:
            async with InfluxClient(url, self.cfg.database, secret(ex.get("user_ref") or ""),
                                    secret(ex.get("password_ref") or "")) as c:
                field = next(iter(spec["fields"].values()))
                rows = await c.query(f"SELECT {q_ident(field)} FROM {q_ident(spec['measurement'])} ORDER BY time DESC LIMIT 1")
        except InfluxError as e:
            raise ReadError(str(e)) from None
        t = rows[0]["values"][0][0] if rows and rows[0].get("values") else None
        return ([Metric("influx_write_age_s", max(0.0, time.time() - t))] if t else []), {}


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
    return None
