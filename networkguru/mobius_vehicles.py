"""Vehicles straight from Mobius (0.9.7).

Every vehicle Mobius has heard from in the last KEEP_S becomes a vehicle of the dashboard by itself - its
own card, map pin and VCU / GNSS values - with what Mobius says it is (the map's `vehicle` values: IP, MAC,
make, model, type) and its IP pinged ("Vehicle IP", type host). Not the Mobius simulator's vehicles
(drivers/mobius.py `simulated`), and not one devices.xlsx already has:

    by name     its Mobius name is a vehicle's Mobius asset or name (as the Mobius reader matches them)
    by IP       its IP is the address of a device of a vehicle (a Mobius name that is a different name in
                devices.xlsx, whose radio has that address): that vehicle gets the Mobius asset name

A vehicle of the inventory matched either way also gets VCU / GNSS when it has none, and its Mobius IP
pinged when that is none of its devices' addresses. devices.xlsx always wins: once it has a vehicle, the
one added from Mobius is dropped (one Events line), as with the scan's copies (found.py).

What Mobius said last is kept in <data>/state/mobius_vehicles.json, so the vehicles are there at once after
a restart; one unheard of for KEEP_S is dropped there. The entries are worked out again at every load
(found.load_site_inventory) - in added.json's shape, merged by found.merge_added.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .drivers.mobius import simulated
from .state import StateFile

KEEP_S = 7 * 86400
SAVE_EVERY_S = 600.0
INFO = ("ip", "mac", "make", "model", "type", "comms")


def norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:40].strip("-") or "x"


class MobiusVehicles:
    def __init__(self, data_dir=None, clock=time.time):
        self.clock = clock
        self.file = StateFile(Path(data_dir) / "state" / "mobius_vehicles.json", {"vehicles": {}, "told": {}}) \
            if data_dir else None
        st = self.file.data if self.file else {}
        self.data: dict[str, dict] = {str(k): dict(v) for k, v in (st.get("vehicles") or {}).items() if isinstance(v, dict)}
        self.told: dict[str, str] = dict(st.get("told") or {})       # name -> what Events last said about it
        self.groups: dict[str, str] = {}                             # Mobius name -> group id (the last load)
        self.auto: set[str] = set()                                  # names that are vehicles of their own
        self.saved_at = 0.0

    # -- what Mobius said ----------------------------------------------------------------------
    def update(self, fleet, now: float | None = None) -> bool:
        """Takes in the Mobius reader's vehicles (positions, link, identity). True when the inventory needs
        building again: a vehicle to add, or a new IP."""
        now = self.clock() if now is None else now
        changed = False
        names = set(fleet.positions.data) | set(fleet.identity)
        for name in names:
            pos = fleet.positions.data.get(name) or {}
            info = fleet.identity.get(name) or {}
            last = max([pos.get(k) or 0.0 for k in ("heard", "t", "link_t")] + [info.get("t") or 0.0])
            if now - last > KEEP_S:
                continue
            e = self.data.setdefault(name, {"first": now})
            old_ip = e.get("ip")
            e["last"] = max(e.get("last", 0.0), last)
            for k in INFO:
                if info.get(k):
                    e[k] = info[k]
            e["simulated"] = simulated(name, info)
            if self._wanted(name, e) and (name not in self.groups or e.get("ip") != old_ip):
                changed = True
        self.data = {n: e for n, e in self.data.items() if now - e.get("last", 0.0) <= KEEP_S}
        if changed or now - self.saved_at >= SAVE_EVERY_S:
            self.save()
            self.saved_at = now
        return changed

    def _wanted(self, name: str, e: dict) -> bool:
        return not e.get("simulated")

    def save(self) -> None:
        if self.file:
            self.file.update(vehicles=self.data, told=self.told)

    # -- into the inventory ---------------------------------------------------------------------
    def entries(self, raw: dict, now: float | None = None) -> tuple[list[dict], list[str]]:
        """(entries in added.json's shape for found.merge_added, Events lines) for this site inventory."""
        now = self.clock() if now is None else now
        vehicles = raw.get("vehicles") or []
        by_name: dict[str, dict] = {}
        by_ip: dict[str, dict] = {}
        ids = {g.get("id") for key in ("vehicles", "control_room", "infrastructure") for g in raw.get(key) or []}
        for g in vehicles:
            for n in (g.get("mobius_asset"), g.get("name")):
                if n:
                    by_name.setdefault(norm(n), g)
            for c in g.get("components") or g.get("devices") or []:
                if c.get("host"):
                    by_ip.setdefault(str(c["host"]).split(":")[0], g)
        out: list[dict] = []
        lines: list[str] = []
        groups: dict[str, str] = {}
        auto: set[str] = set()
        for name, e in sorted(self.data.items()):
            if now - e.get("last", 0.0) > KEEP_S or not self._wanted(name, e):
                continue
            ip = e.get("ip")
            what = " · ".join(x for x in (e.get("type"), e.get("model"), ip) if x)
            g = by_name.get(norm(name))
            how = "name"
            if g is None and ip and ip in by_ip:
                g, how = by_ip[ip], "ip"
            if g is not None:                                    # devices.xlsx has it
                gid = g["id"]
                groups[name] = gid
                types = {c.get("type") for c in g.get("components") or g.get("devices") or []}
                hosts = {str(c.get("host")).split(":")[0] for c in g.get("components") or g.get("devices") or [] if c.get("host")}
                group = {"id": gid, "name": g.get("name") or gid, "kind": "vehicle"}
                if how == "ip" and not g.get("mobius_asset"):
                    group["mobius_asset"] = name
                for typ, nm in (("vcu", "VCU"), ("gnss", "GNSS")):
                    if typ not in types:
                        out.append(self._entry(group, f"{gid}-{typ}", typ, nm))
                if ip and ip not in hosts and not ip.startswith("127."):
                    out.append(self._entry(group, f"{gid}-mobius-ip", "host", "Vehicle IP", host=ip, optional=True))
                if group.get("mobius_asset"):                    # the Mobius name, even with nothing to add
                    out.append({"key": f"mobius:{name}:link", "tab": "vehicles", "group": group, "component": None})
                said = f"linked:{gid}"
                if self.told.get(name) != said:
                    if self.told.get(name) == "auto":
                        lines.append(f"{name}: devices.xlsx has it now ({g.get('name')}) - the vehicle added from Mobius "
                                     "is removed")
                    elif how == "ip":
                        lines.append(f"{g.get('name')} is {name} in Mobius (the same IP, {ip})")
                    self.told[name] = said
                continue
            gid = f"mobius-{slug(name)}"
            n = 2
            while gid in ids:
                gid, n = f"mobius-{slug(name)}-{n}", n + 1
            ids.add(gid)
            groups[name] = gid
            auto.add(name)
            group = {"id": gid, "name": name, "kind": "vehicle", "mobius_asset": name}
            if e.get("model"):
                group["model"] = e["model"]
            out.append(self._entry(group, f"{gid}-vcu", "vcu", "VCU"))
            out.append(self._entry(group, f"{gid}-gnss", "gnss", "GNSS"))
            if ip and not ip.startswith("127."):
                out.append(self._entry(group, f"{gid}-ip", "host", "Vehicle IP", host=ip, mac=e.get("mac"), optional=True))
            if self.told.get(name) != "auto":
                lines.append(f"Added from Mobius: {name}" + (f" ({what})" if what else ""))
                self.told[name] = "auto"
        self.groups, self.auto = groups, auto
        self.save()
        return out, lines

    @staticmethod
    def _entry(group: dict, cid: str, typ: str, name: str, host: str | None = None, mac: str | None = None,
               optional: bool = False) -> dict:
        comp = {"id": cid, "type": typ, "name": name}
        if host:
            comp["host"] = host
        if optional:                                 # its ping never makes a running vehicle Offline
            comp["optional"] = True
        if mac and re.fullmatch(r"[0-9A-Fa-f]{1,2}([-:][0-9A-Fa-f]{1,2}){5}", mac):
            comp["mac"] = ":".join(x.zfill(2) for x in re.split(r"[-:]", mac.lower()))
        return {"key": f"mobius:{cid}", "tab": "vehicles", "group": dict(group), "component": comp}

    # -- what the pages show --------------------------------------------------------------------
    def public(self, now: float | None = None) -> dict:
        """{"groups": {group id: identity}, "others": [identity of the ones with no vehicle - the simulator's]}.
        identity = {name, ip, mac, make, model, type, comms, last, simulated, auto}."""
        now = self.clock() if now is None else now
        groups, others = {}, []
        for name, e in sorted(self.data.items()):
            x = {"name": name, **{k: e.get(k) for k in INFO}, "last": e.get("last"),
                 "simulated": bool(e.get("simulated")), "auto": name in self.auto}
            gid = self.groups.get(name)
            if gid:
                groups[gid] = x
            else:
                others.append(x)
        return {"groups": groups, "others": others}

    def counts(self) -> dict:
        """For the problem report - never a name or an address."""
        return {"known": len(self.data), "added": len(self.auto), "linked": len(self.groups) - len(self.auto),
                "simulated": sum(1 for e in self.data.values() if e.get("simulated"))}
