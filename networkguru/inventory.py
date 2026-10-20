"""inventory.yaml: the site, its control room, vehicles and infrastructure locations.

Everything is a Group (a vehicle, a trailer/tower, the Mobius server, a client desk) holding
Components (one device or one data feed each). Secrets are never in the inventory - a
component names them with `*_ref` fields that are looked up in the server's secrets.env.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from .catalog import TYPES, VEHICLE_RADIO_TYPES, VEHICLE_ROUTER_TYPES

TABS = (("vehicles", "Vehicles"), ("control", "Control Room"), ("infrastructure", "Infrastructure"))
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Component(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    type: str
    name: str | None = None
    host: str | None = None
    target: str | None = None          # host:port when the API is not on `host` (e.g. Starlink gRPC via NAT)
    model: str | None = None
    source: str | None = None          # "mobius" = read from Mobius InfluxDB
    depends_on: str | None = None      # upstream component id (default worked out below)
    uplink: bool = False               # this device carries the location's traffic
    critical: bool = True              # False = can never make its group worse than Warning
    checks: list[str] = Field(default_factory=list)      # e.g. ["tcp:2101"]
    rules: dict[str, dict[str, Any]] = Field(default_factory=dict)   # per-metric threshold overrides
    sim: dict[str, Any] = Field(default_factory=dict)    # simulator knobs (ignored on real devices)
    mobius_machine_id: str | None = None
    # identity - stays the same when the network moves to DHCP
    serial: str | None = None          # e.g. Rajant "FE1-2255X-100001"
    mac: str | None = None             # normalised aa:bb:cc:dd:ee:ff
    hostname: str | None = None
    address: str = "static"            # static | dhcp  (dhcp: `host` is only the last-known address)
    # point-to-point links (Tachyon 60 GHz pairs share a link name)
    link: str | None = None            # e.g. "COW-T46"
    role: str | None = None            # master | slave
    install_rssi: float | None = None  # signal at install; warnings key off it
    port: str | None = None            # switch port it hangs off, e.g. "Port 3"
    notes: str | None = None

    @property
    def label(self) -> str:
        return self.name or TYPES.get(self.type, TYPES["host"]).label

    def secret_refs(self) -> dict[str, str]:
        extra = self.model_extra or {}
        return {k: v for k, v in extra.items() if k.endswith("_ref") and isinstance(v, str) and v}


class Group(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    id: str
    name: str
    kind: str = ""                     # trailer | tower | building | repeater | core | server | client | vehicle
    components: list[Component] = Field(default_factory=list,
                                        validation_alias=AliasChoices("components", "devices"))
    lat: float | None = None
    lon: float | None = None
    mobius_asset: str | None = None    # vehicles: the asset name in Mobius
    access: str | None = None          # vehicles: rajant | celona
    model: str | None = None           # vehicles: truck model text
    expected_online: str | None = None # "Mon-Fri 06:00-18:00" | "always"; offline inside = Critical
    offline_severity: str | None = None  # critical | offline (default by tab/kind)
    sim: dict[str, Any] = Field(default_factory=dict)


class Site(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    name: str
    timezone: str = "UTC"
    lat: float = 40.76                 # Salt Lake City, when a site gives none
    lon: float = -111.89


class MobiusCfg(BaseModel):
    model_config = ConfigDict(extra="allow")
    influx_url: str | None = None
    version: str = "auto"
    database: str | None = None
    asset_tag: str | None = None
    token_ref: str | None = None


class InventoryFile(BaseModel):
    model_config = ConfigDict(extra="allow")
    site: Site
    defaults: dict[str, Any] = Field(default_factory=dict)
    mobius: MobiusCfg = Field(default_factory=MobiusCfg)
    control_room: list[Group] = Field(default_factory=list)
    infrastructure: list[Group] = Field(default_factory=list)
    vehicles: list[Group] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# schedules ("Mon-Fri 06:00-18:00")
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Schedule:
    days: frozenset[int]
    start_min: int
    end_min: int

    @classmethod
    def parse(cls, text: str | None) -> "Schedule | None":
        if not text:
            return None
        t = text.strip().lower()
        if t == "always":
            return cls(frozenset(range(7)), 0, 24 * 60)
        m = re.match(r"^([a-z]{3})(?:-([a-z]{3}))?\s+(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$", t)
        if not m or m.group(1) not in DAYS or (m.group(2) and m.group(2) not in DAYS):
            raise ValueError(f"schedule {text!r}: use e.g. 'Mon-Fri 06:00-18:00' or 'always'")
        a = DAYS.index(m.group(1))
        b = DAYS.index(m.group(2)) if m.group(2) else a
        days = frozenset((a + i) % 7 for i in range((b - a) % 7 + 1))
        return cls(days, int(m.group(3)) * 60 + int(m.group(4)), int(m.group(5)) * 60 + int(m.group(6)))

    def active(self, local: dt.datetime) -> bool:
        minute = local.hour * 60 + local.minute
        return local.weekday() in self.days and self.start_min <= minute < self.end_min


# ---------------------------------------------------------------------------
# the loaded inventory with lookup helpers
# ---------------------------------------------------------------------------

class InventoryError(Exception):
    pass


MAC_HEX = re.compile(r"^[0-9a-f]{12}$")


def normalise_mac(text: str) -> str:
    """'AA-BB-CC-DD-EE-FF', 'aabb.ccdd.eeff', 'AABBCCDDEEFF' -> 'aa:bb:cc:dd:ee:ff'."""
    h = re.sub(r"[^0-9a-fA-F]", "", text or "").lower()
    if not MAC_HEX.match(h):
        raise ValueError(f"MAC {text!r} is not 12 hex digits")
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


class Inventory:
    def __init__(self, data: InventoryFile, source: str = ""):
        self.data = data
        self.source = source
        self.site = data.site
        self.groups: dict[str, Group] = {}
        self.group_tab: dict[str, str] = {}
        self.comps: dict[str, Component] = {}
        self.comp_group: dict[str, str] = {}
        self.schedules: dict[str, Schedule | None] = {}
        self.warnings: list[str] = []
        # redundant: trailers reached over Tachyon AND the Rajant mesh; tachyon: 60 GHz only
        self.backhaul = (data.defaults or {}).get("backhaul")
        problems: list[str] = []
        for tab, groups in (("vehicles", data.vehicles), ("control", data.control_room),
                            ("infrastructure", data.infrastructure)):
            for g in groups:
                self._add_group(tab, g, problems)
        self._identity_checks(problems)
        if problems:
            raise InventoryError("\n".join(problems))
        self._apply_type_defaults()
        self._default_dependencies()
        self._check_cycles()
        self._link_checks()

    # -- identity, type defaults, links ------------------------------------
    def _identity_checks(self, problems: list[str]) -> None:
        seen_serial: dict[str, str] = {}
        seen_mac: dict[str, str] = {}
        for c in self.comps.values():
            if c.mac:
                try:
                    c.mac = normalise_mac(c.mac)
                except ValueError as e:
                    problems.append(f"{c.id}: {e}")
            if c.address not in ("static", "dhcp"):
                problems.append(f"{c.id}: address must be static or dhcp, not {c.address!r}")
            if c.address == "dhcp" and not (c.serial or c.mac or c.hostname):
                problems.append(f"{c.id}: a DHCP device needs a serial, MAC or hostname so it can be found again")
            if c.role and c.role not in ("master", "slave"):
                problems.append(f"{c.id}: role must be master or slave")
            for value, seen, what in ((c.serial, seen_serial, "serial"), (c.mac, seen_mac, "MAC")):
                if value:
                    if value in seen:
                        problems.append(f"{c.id}: {what} {value} is also on {seen[value]}")
                    seen[value] = c.id
            if (c.address == "static" and not c.host and not c.target and c.source != "mobius"
                    and TYPES.get(c.type) and TYPES[c.type].ping):
                self.warnings.append(f"{c.id}: no IP address yet - it will show Unknown until one is added")

    def _apply_type_defaults(self) -> None:
        """defaults.types.<type>: {user_ref: ..., ...} fills fields a component doesn't set."""
        per_type = (self.data.defaults or {}).get("types") or {}
        for c in self.comps.values():
            for k, v in (per_type.get(c.type) or {}).items():
                extra = c.model_extra if c.model_extra is not None else {}
                if k in type(c).model_fields:
                    if getattr(c, k) in (None, "", [], {}):
                        setattr(c, k, v)
                elif k not in extra:
                    extra[k] = v
            if c.type == "tachyon" and c.install_rssi is not None and "tachyon_rssi_dbm" not in c.rules:
                base = float(c.install_rssi)
                c.rules["tachyon_rssi_dbm"] = {"low_warn": base - 6, "low_crit": base - 12}

    def _check_cycles(self) -> None:
        for start in self.comps:
            seen, cur = set(), start
            while cur:
                if cur in seen:
                    raise InventoryError(f"{start}: depends_on goes round in a circle ({' -> '.join(seen)})")
                seen.add(cur)
                cur = self.comps[cur].depends_on

    def _link_checks(self) -> None:
        links: dict[str, list[str]] = {}
        for c in self.comps.values():
            if c.link:
                links.setdefault(c.link, []).append(c.id)
        for name, ids in links.items():
            if len(ids) != 2:
                self.warnings.append(f"link {name}: expected 2 ends, found {len(ids)} ({', '.join(ids)})")
        self.links = links

    def link_partner(self, comp_id: str) -> str | None:
        c = self.comps[comp_id]
        if not c.link:
            return None
        return next((x for x in self.links.get(c.link, []) if x != comp_id), None)

    def root_cause(self, comp_id: str, down: set[str]) -> str:
        """Walk depends_on up while devices are down; return the highest one that is down."""
        cur, root = self.comps[comp_id].depends_on, None
        guard = 0
        while cur and cur in down and guard < 64:
            root, cur, guard = cur, self.comps[cur].depends_on, guard + 1
        return root or self.comps[comp_id].depends_on

    # -- building ---------------------------------------------------------
    def _add_group(self, tab: str, g: Group, problems: list[str]) -> None:
        if not ID_RE.match(g.id):
            problems.append(f"group id {g.id!r}: use lower-case letters, digits, '-' or '_'")
        if g.id in self.groups or g.id in self.comps or g.id in dict(TABS):
            problems.append(f"duplicate id {g.id!r}")
        self.groups[g.id] = g
        self.group_tab[g.id] = tab
        try:
            self.schedules[g.id] = Schedule.parse(g.expected_online)
        except ValueError as e:
            problems.append(f"{g.id}: {e}")
        if not g.components:
            problems.append(f"{g.id}: has no components/devices")
        for c in g.components:
            if not ID_RE.match(c.id):
                problems.append(f"component id {c.id!r}: use lower-case letters, digits, '-' or '_'")
            if c.id in self.comps or c.id in self.groups:
                problems.append(f"duplicate id {c.id!r}")
            if c.type not in TYPES:
                problems.append(f"{c.id}: unknown type {c.type!r} (known: {', '.join(sorted(TYPES))})")
            elif TYPES[c.type].source == "mobius" and c.source is None:
                c.source = "mobius"
            self.comps[c.id] = c
            self.comp_group[c.id] = g.id

    def _default_dependencies(self) -> None:
        for gid, g in self.groups.items():
            tab = self.group_tab[gid]
            if tab == "vehicles":
                radio = next((c for c in g.components if c.type in VEHICLE_RADIO_TYPES), None)
                router = next((c for c in g.components if c.type in VEHICLE_ROUTER_TYPES), None)
                for c in g.components:
                    if c.depends_on:
                        continue
                    if c is radio:
                        continue
                    if c is router:
                        c.depends_on = radio.id if radio else None
                    else:
                        c.depends_on = (router or radio).id if (router or radio) else None
            elif tab == "infrastructure":
                switch = next((c for c in g.components if c.type == "switch"), None)
                if switch:                    # the switch powers and connects everything on a trailer
                    for c in g.components:
                        if not c.depends_on and c is not switch:
                            c.depends_on = switch.id
                    continue
                up = next((c for c in g.components if c.uplink), None) or \
                     next((c for c in g.components if c.type == "rajant"), None)
                for c in g.components:
                    if not c.depends_on and up is not None and c is not up:
                        c.depends_on = up.id
            else:
                svc = next((c for c in g.components if c.type == "mobius_server"), None)
                for c in g.components:
                    if not c.depends_on and svc is not None and c.type == "mobius_ntrip":
                        c.depends_on = svc.id
        for c in self.comps.values():
            if c.depends_on and c.depends_on not in self.comps:
                raise InventoryError(f"{c.id}: depends_on {c.depends_on!r} is not a component id")

    # -- lookups ----------------------------------------------------------
    def tab_groups(self, tab: str) -> list[Group]:
        return [g for gid, g in self.groups.items() if self.group_tab[gid] == tab]

    def group_of(self, comp_id: str) -> Group:
        return self.groups[self.comp_group[comp_id]]

    def offline_severity(self, group_id: str) -> str:
        g = self.groups[group_id]
        if g.offline_severity in ("critical", "offline"):
            return g.offline_severity
        tab = self.group_tab[group_id]
        if tab == "vehicles" or (tab == "control" and g.kind == "client"):
            return "offline"
        return "critical"

    def expected_online(self, group_id: str, local: dt.datetime) -> bool:
        sch = self.schedules.get(group_id)
        return bool(sch and sch.active(local))

    def node_name(self, node_id: str) -> str:
        if node_id in self.comps:
            c = self.comps[node_id]
            return f"{self.group_of(node_id).name} · {c.label}"
        if node_id in self.groups:
            return self.groups[node_id].name
        return dict(TABS).get(node_id, node_id)

    def missing_secrets(self, env: dict[str, str] | None = None) -> list[str]:
        """Names (never values) of secret refs not set in the environment or the server's secrets.env."""
        if env is None:
            from .config import secret
            has = lambda name: bool(secret(name))  # noqa: E731
        else:
            has = lambda name: bool(env.get(name))  # noqa: E731
        names = set()
        for c in self.comps.values():
            for ref in c.secret_refs().values():
                if not has(ref):
                    names.add(ref)
        if self.data.mobius.token_ref and not has(self.data.mobius.token_ref):
            names.add(self.data.mobius.token_ref)
        return sorted(names)

    def public_tree(self) -> dict:
        """Inventory for the browser: no *_ref names, no sim knobs."""
        tabs = []
        for tab, title in TABS:
            groups = []
            for g in self.tab_groups(tab):
                groups.append({
                    "id": g.id, "name": g.name, "kind": g.kind or ("vehicle" if tab == "vehicles" else ""),
                    "access": g.access, "model": g.model, "mobius_asset": g.mobius_asset,
                    "expected_online": g.expected_online, "offline_severity": self.offline_severity(g.id),
                    "components": [{
                        "id": c.id, "type": c.type, "name": c.label, "host": c.host, "model": c.model,
                        "source": c.source or "device", "depends_on": c.depends_on, "critical": c.critical,
                        "serial": c.serial, "mac": c.mac, "hostname": c.hostname, "address": c.address,
                        "link": c.link, "role": c.role, "install_rssi": c.install_rssi, "port": c.port,
                        "notes": c.notes, "partner": self.link_partner(c.id),
                    } for c in g.components],
                })
            tabs.append({"id": tab, "title": title, "groups": groups})
        return {"site": {"id": self.site.id, "name": self.site.name, "timezone": self.site.timezone,
                         "lat": self.site.lat, "lon": self.site.lon, "backhaul": self.backhaul},
                "tabs": tabs}


def load_inventory(path: str | Path) -> Inventory:
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise InventoryError(f"cannot read {p}: {e}") from e
    return parse_inventory(raw, str(p))


def parse_inventory(raw: dict, source: str = "") -> Inventory:
    try:
        data = InventoryFile.model_validate(raw)
    except ValidationError as e:
        lines = []
        for err in e.errors():
            where = ".".join(str(x) for x in err["loc"])
            lines.append(f"{where}: {err['msg']}")
        raise InventoryError("\n".join(lines)) from None
    return Inventory(data, source)
