"""The discovery scan on the server (0.9.0): the review list, devices that moved, devices an admin added.

Files in <data>/state/ (the site's inventory.yaml stays read-only):
    discovery.json   the last scan, the review list (new / changed / moved / added items), ignored keys,
                     identities learned from the scan (a Mobius PC's NetBIOS name + MAC ...), recent moves
    addresses.json   devices followed to a new address: {comp id: {address, was, ts, how}} - applied at
                     start; dropped when the inventory itself has the new address (or changed otherwise)
    added.json       devices an admin added from the review list, in inventory format - merged into the
                     inventory at start; one the sheet now also has (serial / MAC / address) is left out
"""

from __future__ import annotations

import asyncio
import copy
import csv
import io
import logging
import time
from pathlib import Path

from .catalog import TYPES
from .daily import DailyJob, local_date
from .discovery import (ROLE_LABEL, SCAN_LIMIT_S, Scanner, ScanError, match, norm_mac, ranges, site_logins)
from .inventory import parse_inventory, load_raw
from .state import StateFile

log = logging.getLogger("networkguru.discovery")

TAB_KEYS = {"vehicles": "vehicles", "control": "control_room", "infrastructure": "infrastructure"}
KINDS = ["vehicle", "trailer", "cow", "tower", "building", "repeater", "core", "server", "client"]
KEEP_NEW_S = 7 * 86400          # a new device not seen again stays on the list this long
MAX_OTHERS = 300


def tab_of_kind(kind: str) -> str:
    return "vehicles" if kind == "vehicle" else "control" if kind in ("server", "client") else "infrastructure"


def _state(data_dir, name: str, defaults: dict | None = None) -> StateFile:
    return StateFile(Path(data_dir) / "state" / name, defaults or {})


# -- the inventory as the server uses it -------------------------------------------------------
def merge_added(raw: dict, entries: list[dict]) -> tuple[dict, list[str]]:
    """The site inventory + the devices added from the scan (a copy; the file is never changed)."""
    raw = copy.deepcopy(raw)
    notes: list[str] = []
    ids, serials, macs, places = set(), set(), set(), set()
    for key in TAB_KEYS.values():
        raw[key] = raw.get(key) or []
        for g in raw[key]:
            ids.add(g.get("id"))
            for c in g.get("components") or g.get("devices") or []:
                ids.add(c.get("id"))
                if c.get("serial"):
                    serials.add(str(c["serial"]).strip().lower())
                if norm_mac(c.get("mac")):
                    macs.add(norm_mac(c.get("mac")))
                if c.get("host"):
                    places.add((str(c["host"]), c.get("type")))
    for e in entries:
        c = dict(e.get("component") or {})
        if not c.get("id") or not c.get("type"):
            continue
        dup = (c.get("serial") and str(c["serial"]).strip().lower() in serials) or \
              (norm_mac(c.get("mac")) and norm_mac(c.get("mac")) in macs) or (c.get("host"), c.get("type")) in places
        if dup:
            notes.append(f"{c.get('name') or c['id']} ({c.get('host')}) is in the site inventory now - the copy added "
                         "from the network scan is left out")
            continue
        if c["id"] in ids:
            continue
        groups = raw[TAB_KEYS.get(e.get("tab"), "infrastructure")]
        gid = e["group"]["id"]
        g = next((x for x in groups if x.get("id") == gid), None)
        if g is None:
            if gid in ids:                          # the id is used on another tab
                continue
            g = {"id": gid, "name": e["group"].get("name") or gid, "kind": e["group"].get("kind") or "", "components": []}
            groups.append(g)
            ids.add(gid)
        (g["components"] if "components" in g else g.setdefault("devices", [])).append(c)
        ids.add(c["id"])
    return raw, notes


def apply_overlay(inv, overlay: dict) -> tuple[dict, list[str]]:
    """Point followed devices at their new address. Returns the entries still needed."""
    keep, notes = {}, []
    for cid, e in overlay.items():
        c = inv.comps.get(cid)
        if c is None or not e.get("address"):
            continue
        if c.host == e["address"]:
            continue                                         # the inventory has caught up
        if e.get("was") is not None and c.host != e["was"]:
            notes.append(f"{inv.node_name(cid)}: the inventory now says {c.host} - its address from the scan "
                         f"({e['address']}) is dropped")
            continue
        c.host = e["address"]
        keep[cid] = e
    return keep, notes


def load_site_inventory(path, data_dir):
    """(inventory, the addresses the files gave each device, notes) - live mode's inventory."""
    added = _state(data_dir, "added.json", {"entries": []})
    raw, notes = merge_added(load_raw(path), added.get("entries") or [])
    inv = parse_inventory(raw, str(path))
    base = {cid: c.host for cid, c in inv.comps.items()}
    ov = _state(data_dir, "addresses.json")
    keep, more = apply_overlay(inv, dict(ov.data))
    if keep != ov.data:
        ov.replace(keep)
    return inv, base, notes + more


# -- the service -------------------------------------------------------------------------------
class Discovery:
    def __init__(self, rt, ports=None):
        self.rt = rt
        d = rt.settings.data_dir
        self.state = _state(d, "discovery.json", {"last": None, "date": None, "items": {}, "ignored": [],
                                                  "learned": {}, "moves": [], "others": []})
        self.overlay = _state(d, "addresses.json")
        self.added = _state(d, "added.json", {"entries": []})
        self.ports = ports
        self.allow_loopback = False             # tests and the lab only: fake devices on 127.0.0.x
        self.task: asyncio.Task | None = None
        self.scanner: Scanner | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def daily_job(self) -> DailyJob:
        return DailyJob("discovery", self.rt.pipeline.tz, lambda: self.rt.settings_state.get("discovery_time", "02:00") or "",
                        lambda date: self.state.get("date") == date, lambda date, late: self.scan("nightly"),
                        catchup_s=3 * 3600)

    def nets(self) -> list:
        """What a scan covers: the /24s around the inventory, the Sites sheet Subnets and the Admin page's
        'Also scan these networks'."""
        return ranges(self.rt.inv, extra=self.rt.settings_state.get("discovery_ranges") or [],
                      allow_loopback=self.allow_loopback)

    def start(self, who: str) -> bool:
        if self.running:
            return False
        self.task = asyncio.create_task(self.scan(who))
        return True

    async def scan(self, who: str = "nightly") -> dict:
        t0 = time.time()
        last = {"ts": t0, "who": who, "ranges": [], "addresses": 0, "answered": 0, "duration_s": 0,
                "counts": {}, "error": None}
        try:
            nets = self.nets()
            last["ranges"] = [str(n) for n in nets]
            kw = {"ports": self.ports} if self.ports else {}
            self.scanner = Scanner(site_logins(self.rt.inv), can_ping=self.rt.can_ping, **kw)
            found, tried = await asyncio.wait_for(self.scanner.run(nets), SCAN_LIMIT_S)
            last["addresses"], last["answered"] = tried, len(found)
            last["counts"] = self.apply(found, t0)
        except ScanError as e:
            last["error"] = str(e)
        except (TimeoutError, asyncio.TimeoutError):
            last["error"] = f"the scan took longer than {SCAN_LIMIT_S / 60:.0f} minutes and was stopped"
        except Exception as e:  # noqa: BLE001
            log.exception("discovery scan failed")
            last["error"] = f"scan error ({type(e).__name__})"
        last["duration_s"] = round(time.time() - t0)
        self.state.update(last=last, date=local_date(t0, self.rt.pipeline.tz))
        c = last["counts"]
        text = (f"Network scan: {last['error']}" if last["error"] else
                f"Network scan: {c.get('new', 0)} new, {c.get('moved', 0)} moved, {c.get('changed', 0)} changed "
                f"({last['answered']} of {last['addresses']} addresses answered)")
        if self.rt.events is not None:
            self.rt.events.add_system_event(time.time(), "discovery", text,
                                            3 if last["error"] or c.get("new") or c.get("changed") else 0,
                                            None if who == "nightly" else who)
        return last

    def apply(self, found, now: float) -> dict:
        """Sort a scan's finds into the review list; follow moved devices; learn identities."""
        learned = dict(self.state.get("learned") or {})
        prev = dict(self.state.get("items") or {})
        items, counts, moves = {}, {}, list(self.state.get("moves") or [])
        for it in match(found, self.rt.inv, learned):
            old = prev.get(it["key"], {})
            it["first"], it["last"] = old.get("first", now), now
            if old.get("outcome") == "added" and it["outcome"] in ("new", "changed"):
                it["outcome"], it["comp"] = "added", old.get("comp")
            learn = it.pop("learn", None)
            if learn and it["comp"]:
                learned[it["comp"]] = {**learned.get(it["comp"], {}), **learn}
            if it["outcome"] == "moved":
                how = (f"serial {it['serial']}" if it["serial"] else f"MAC {it['mac']}" if it["mac"] else f"name {it['name']}")
                self.follow(it["comp"], it["address"], how, now)
                moves.append({"comp": it["comp"], "name": self.rt.inv.node_name(it["comp"]), "old": it["old"],
                              "new": it["address"], "ts": now, "how": how})
            counts[it["outcome"]] = counts.get(it["outcome"], 0) + 1
            items[it["key"]] = it
        for key, old in prev.items():                     # new ones not seen this time stay a while
            if key not in items and old.get("outcome") in ("new", "changed", "added") and now - old.get("last", 0) < KEEP_NEW_S:
                items[key] = old
        others = [{"address": f.address, "name": f.name, "mac": norm_mac(f.mac), "how": f.how[:3]}
                  for f in found if not f.roles][:MAX_OTHERS]
        self.state.update(items=items, learned=learned, moves=moves[-50:], others=others)
        return counts

    def follow(self, cid: str, address: str, how: str, now: float | None = None) -> None:
        comp = self.rt.inv.comps.get(cid)
        if comp is None:
            return
        comp.host = address
        poller = self.rt.scheduler.poller_of(cid) if self.rt.scheduler else None
        if poller is not None and hasattr(poller, "retarget"):
            poller.retarget(address)
        ov = dict(self.overlay.data)
        ov[cid] = {"address": address, "was": self.rt.base_hosts.get(cid), "ts": now or time.time(), "how": how}
        self.overlay.replace(ov)
        log.info("discovery: %s followed to a new address (%s)", cid, how)

    # -- the admin's actions ----------------------------------------------------------------------
    def ignore(self, key: str, on: bool = True) -> None:
        ign = [k for k in (self.state.get("ignored") or []) if k != key]
        if on:
            ign.append(key)
        self.state.update(ignored=ign)

    def add(self, key: str, *, name: str, ctype: str, group_id: str | None = None, group_name: str = "",
            kind: str = "", who: str = "") -> str:
        from .importers.build import CELL_DEFAULTS, TYPE_CHECKS, slug
        items = dict(self.state.get("items") or {})
        it = items.get(key)
        if it is None:
            raise ValueError("that device is not on the list (scan again)")
        if it.get("outcome") not in ("new", "changed"):
            raise ValueError("only a new device can be added")
        name = (name or "").strip()[:60]
        if not name:
            raise ValueError("give it a name")
        if ctype not in TYPES:
            raise ValueError(f"unknown type '{ctype}'")
        inv = self.rt.inv
        ids = set(inv.comps) | set(inv.groups)
        if group_id:
            if group_id not in inv.groups:
                raise ValueError("that group is not in the inventory")
            g = inv.groups[group_id]
            group = {"id": g.id, "name": g.name, "kind": g.kind}
            tab = inv.group_tab[group_id]
        else:
            group_name = (group_name or "").strip()[:60]
            if not group_name:
                raise ValueError("pick a group or give the new group a name")
            if kind not in KINDS:
                raise ValueError(f"kind must be one of {', '.join(KINDS)}")
            gid, i = slug(group_name), 2
            while gid in ids:
                gid, i = f"{slug(group_name)}-{i}", i + 1
            group, tab = {"id": gid, "name": group_name, "kind": kind}, tab_of_kind(kind)
            ids.add(gid)
        base = slug(f"{group['id']}-{name}", 62)
        cid, i = base, 2
        while cid in ids:
            cid, i = f"{base}-{i}"[:62], i + 1
        comp = {"id": cid, "type": ctype, "name": name, "host": it["address"]}
        for k in ("serial", "mac", "model"):
            if it.get(k):
                comp[k] = it[k]
        if it.get("name") and ctype in ("mobius_server", "influxdb", "mobius_client", "host", "gnss_base"):
            comp["hostname"] = it["name"]
        if ctype in TYPE_CHECKS:
            comp["checks"] = list(TYPE_CHECKS[ctype])
        if ctype == "cell_modem":
            from .drivers.cellular import kind as cell_kind
            comp.update(CELL_DEFAULTS["alms" if cell_kind(comp.get("model")) == "alms" else "snmp"])
        entries = list(self.added.get("entries") or [])
        entries.append({"key": key, "tab": tab, "group": group, "component": comp, "ts": time.time(), "by": who})
        self.added.update(entries=entries)
        new_inv, base_hosts, notes = load_site_inventory(self.rt.settings.inventory, self.rt.settings.data_dir)
        if cid not in new_inv.comps:
            self.added.update(entries=entries[:-1])
            raise ValueError("; ".join(notes) or "it could not be added to the inventory")
        self.rt.swap_inventory(new_inv, base_hosts)
        it["outcome"], it["comp"] = "added", cid
        items[key] = it
        self.state.update(items=items)
        return cid

    # -- what the Admin page shows -------------------------------------------------------------
    def public(self) -> dict:
        st = self.state.data
        ign = set(st.get("ignored") or [])
        items = sorted((dict(v, ignored=k in ign) for k, v in (st.get("items") or {}).items()),
                       key=lambda x: (x["outcome"], tuple(int(p) for p in x["address"].split(".")), x["type"]))
        inv = self.rt.inv
        for x in items:
            x["label"] = ROLE_LABEL.get(x["type"], x["type"])
            x["comp_name"] = inv.node_name(x["comp"]) if x.get("comp") in inv.comps else None
        return {"enabled": True, "running": self.running,
                "progress": dict(self.scanner.progress) if self.running and self.scanner else None,
                "last": st.get("last"), "time": self.rt.settings_state.get("discovery_time", "02:00"),
                "ranges": list(self.rt.settings_state.get("discovery_ranges") or []),
                "items": [x for x in items if x["outcome"] != "known"],
                "known": sum(1 for x in items if x["outcome"] == "known"),
                "moves": list(reversed(st.get("moves") or []))[:20], "others": st.get("others") or [],
                "groups": [{"id": g.id, "name": g.name, "kind": g.kind, "tab": inv.group_tab[g.id]}
                           for g in inv.groups.values()],
                "kinds": KINDS, "types": {k: t.label for k, t in TYPES.items()}}

    def export_csv(self) -> str:
        """The new devices as rows for `import-devices` (then build-inventory), so devices.xlsx catches up."""
        buf = io.StringIO()
        w = csv.DictWriter(buf, ["name", "site", "type", "ip", "serial", "model", "mac", "group", "kind"], lineterminator="\n")
        w.writeheader()
        ign = set(self.state.get("ignored") or [])
        inv = self.rt.inv
        for k, it in sorted((self.state.get("items") or {}).items()):
            if it["outcome"] in ("new", "changed", "added") and k not in ign:
                comp = inv.comps.get(it.get("comp") or "") if it["outcome"] == "added" else None
                group = inv.groups[inv.comp_group[comp.id]] if comp else None
                w.writerow({"name": (comp.name if comp else it.get("name")) or f"{ROLE_LABEL.get(it['type'], it['type'])} {it['address']}",
                            "site": inv.site.id, "type": comp.type if comp else it["type"], "ip": it["address"],
                            "serial": it.get("serial") or "", "model": it.get("model") or "", "mac": it.get("mac") or "",
                            "group": group.name if group else "", "kind": group.kind if group else ""})
        return buf.getvalue()
