"""Fill devices.xlsx from real site data, and turn it into inventory.yaml.

    import_ipam(sheet, rows, site)        IPAM sheet rows (trailers, COW, switches, Cerbo, Tachyon pairs)
    import_rajant(sheet, nodes, site)     BC|Commander snapshot (serial, firmware, MAC, IP, DHCP/static)
    import_csv(sheet, rows, site)         V&V radio-monitor devices.csv (+ optional group/kind columns)
    build_inventory(sheet, site, ...)     -> (inventory dict, report lines)
"""

from __future__ import annotations

import datetime as dt
import re

from ..catalog import TYPES
from .ipam import IpamRow, device_name, device_type, is_spare, location_group
from .rajant_snapshot import RajantNode
from .sheet import Sheet, _in_nets

TAB_OF_KIND = {"vehicle": "vehicles", "server": "control_room", "client": "control_room"}
TACHYON_MODEL = "TNA-303X"          # from the post-install material inventory
# a site's name, time zone, location, InfluxDB and subnets are on the Sites sheet of devices.xlsx;
# these are only used for a site without a Sites row
DEFAULT_TZ = "America/Denver"
DEFAULT_LAT, DEFAULT_LON = 40.76, -111.89      # Salt Lake City
# ports checked in live mode, by type (the service the device is there for)
TYPE_CHECKS = {"mobius_server": ["tcp:18080"], "influxdb": ["tcp:8086"], "gnss_base": ["tcp:2101"]}
# login names per device type - values are typed on the server into secrets.env, never stored here
TYPE_DEFAULTS = {
    "rajant": {"snmp_version": "3", "snmp_user_ref": "RAJANT_SNMP_USER", "snmp_auth_ref": "RAJANT_SNMP_AUTH",
               "snmp_priv_ref": "RAJANT_SNMP_PRIV"},
    "tachyon": {"snmp_version": "2c", "snmp_community_ref": "TACHYON_SNMP_RO"},
    "switch": {"snmp_version": "2c", "snmp_community_ref": "SWITCH_SNMP_RO"},
}


def slug(text: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:n].strip("-") or "x"


def classify(name: str, address: str, infra_subnet: str = "") -> str:
    """The kind of a group named in a snapshot or CSV. infra_subnet: the site's Infrastructure
    subnet (Sites sheet) - an unnamed radio there is a tower."""
    low = (name or "").lower()
    if "trailer" in low:
        return "trailer"
    if "building" in low:
        return "building"
    if "repeater" in low:
        return "repeater"
    if "base" in low:
        return "core"
    if "cow" in low:
        return "cow"
    if "dirt track" in low or "tower" in low:
        return "tower"
    if _in_nets(address, infra_subnet):
        return "tower"
    return "vehicle"


# ---------------------------------------------------------------------------
def import_ipam(sheet: Sheet, rows: list[IpamRow], site: str) -> None:
    for r in rows:
        typ = device_type(r.device, r.location)
        gname, kind = location_group(r.location, r.device)
        sheet.upsert_group(site, gname, kind)
        notes = "; ".join(x for x in (r.notes, f"ch {r.channel}" if r.channel else "",
                                       f"cable {r.cable}" if r.cable else "") if x)
        sheet.upsert_device({
            "Site": site, "Group": gname, "Type": typ, "Name": device_name(r.device, typ),
            "Model": TACHYON_MODEL if typ == "tachyon" else "", "IP": r.ip, "Link": r.ssid,
            "Role": {"M": "master", "S": "slave"}.get(r.ms.strip().upper(), ""),
            "Install RSSI": r.rssi if r.rssi is not None else "", "Switch port": r.port,
            "Monitor": "no" if is_spare(r) else "yes", "Notes": notes,
        }, owned=("IP", "Link", "Role", "Install RSSI", "Switch port"))


def import_rajant(sheet: Sheet, nodes: list[RajantNode], site: str) -> None:
    for n in nodes:
        nsite = sheet.site_of(n.address) or site                 # Sites sheet: Subnets
        row = {"Site": nsite, "Type": "rajant", "Serial": n.serial, "Model": n.model, "Firmware": n.firmware,
               "MAC": n.mac, "IP": n.address, "Address": "dhcp" if n.ip_mode == "DHCP" else "static"}
        existing = sheet.find_device(nsite, serial=n.serial, ip=n.address)
        owned = ("IP", "Firmware", "MAC", "Serial", "Address", "Model")
        if existing:
            sheet.upsert_device({**row, "Group": existing["Group"], "Name": existing["Name"]}, owned=owned)
            _place(sheet, nsite, existing["Group"], n)
            continue
        kind = classify(n.name, n.address, _infra(sheet, nsite))
        gname = n.name.strip() or n.serial
        sheet.upsert_group(nsite, gname, kind, **({"Access": "rajant"} if kind == "vehicle" else {}))
        sheet.upsert_device({**row, "Group": gname, "Name": f"Rajant {n.platform or 'BreadCrumb'}"}, owned=owned)
        _place(sheet, nsite, gname, n)


def _place(sheet: Sheet, site: str, group: str, n: RajantNode) -> None:
    """A location's map position from its radio's GPS - only when the sheet has none (a typed one wins).
    Vehicles get none: they move."""
    g = sheet.group(site, group)
    if g is None or g["Kind"].strip().lower() == "vehicle" or g["Lat"] or g["Lon"]:
        return
    if n.lat is None or n.lon is None or (abs(n.lat) < 1e-6 and abs(n.lon) < 1e-6):
        return
    g["Lat"], g["Lon"] = f"{n.lat:.6f}", f"{n.lon:.6f}"
    sheet.log.append(f"~ group {site}/{group}: position from {n.serial}")


def import_csv(sheet: Sheet, rows: list[dict], site: str) -> None:
    for r in rows:
        name = r.get("name", "")
        rsite = (r.get("site") or site).strip()
        if rsite.lower() in ("durability", "durability mesh"):
            rsite = site
        typ = (r.get("type") or "").strip().lower()
        typ = typ if typ in TYPES else device_type(typ or name)
        gname = r.get("group") or name
        kind = r.get("kind") or classify(gname, r.get("ip", ""), _infra(sheet, rsite))
        sheet.upsert_group(rsite, gname, kind, **{"Mobius asset": r.get("mobius_asset", "")})
        sheet.upsert_device({
            "Site": rsite, "Group": gname, "Type": typ, "Name": r.get("device") or name, "Model": r.get("model", ""),
            "Serial": r.get("serial", ""), "MAC": r.get("mac", ""), "IP": r.get("ip", ""),
            "Firmware": r.get("firmware", ""), "Address": r.get("address", ""),
        }, owned=("IP", "Firmware"))


# ---------------------------------------------------------------------------
def build_inventory(sheet: Sheet, site: str, name: str | None = None, timezone: str | None = None,
                    lat: float | None = None, lon: float | None = None,
                    backhaul: str = "redundant") -> tuple[dict, list[str]]:
    """backhaul: 'redundant' - trailers are reached over the Tachyon links AND the Rajant mesh (Mendon);
    'tachyon' - the 60 GHz links are the only path, so trailers chain off each other."""
    if backhaul not in ("redundant", "tachyon"):
        raise ValueError("backhaul must be redundant or tachyon")
    report: list[str] = []
    meta = sheet.site(site) or {}
    if not meta:
        report.append(f"! no Sites row for '{site}' in the sheet - the name, time zone and location are defaults; add one")
    lat = lat if lat is not None else _num(meta.get("Lat"), DEFAULT_LAT, "Lat", report)
    lon = lon if lon is not None else _num(meta.get("Lon"), DEFAULT_LON, "Lon", report)
    influx = (meta.get("Mobius InfluxDB URL") or "").strip()
    groups = [g for g in sheet.groups if g["Site"].strip().lower() == site.lower()]
    devices = [d for d in sheet.devices if d["Site"].strip().lower() == site.lower()]
    known = {g["Group"].strip().lower() for g in groups}
    for d in devices:
        if d["Group"].strip().lower() not in known:
            report.append(f"! device {d['Name']} ({d['IP']}): group '{d['Group']}' is not on the Groups sheet - skipped")
    used_ids: set[str] = set()

    def unique(base: str) -> str:
        i, cand = 2, base
        while cand in used_ids:
            cand, i = f"{base}-{i}"[:62], i + 1
        used_ids.add(cand)
        return cand

    tabs: dict[str, list] = {"vehicles": [], "control_room": [], "infrastructure": []}
    comp_rows: list[tuple[dict, dict, dict]] = []          # (group entry, component entry, sheet row)
    skipped = 0
    for g in groups:
        kind = g["Kind"].strip().lower() or "tower"
        gid = unique(slug(g["Group"]))
        entry: dict = {"id": gid, "name": g["Group"].strip(), "kind": kind, "components": []}
        if kind == "vehicle":
            entry["access"] = (g["Access"] or "rajant").lower()
            if g["Mobius asset"]:
                entry["mobius_asset"] = g["Mobius asset"]
        if g["Expected online"]:
            entry["expected_online"] = g["Expected online"]
        if g["Lat"] or g["Lon"]:
            where = _position(g["Lat"], g["Lon"])
            if where:
                entry["lat"], entry["lon"] = where
            else:
                report.append(f"! {g['Group']}: Lat/Lon '{g['Lat']}' / '{g['Lon']}' is not a position - not on the map")
        for d in devices:
            if d["Group"].strip().lower() != g["Group"].strip().lower():
                continue
            if d["Monitor"].strip().lower() == "no":
                skipped += 1
                continue
            typ = d["Type"].strip().lower()
            if typ not in TYPES:
                report.append(f"! {g['Group']} / {d['Name']}: unknown type '{d['Type']}' - skipped")
                continue
            c: dict = {"id": unique(f"{gid}-{slug(d['Name'], 20)}"[:62]), "type": typ, "name": d["Name"]}
            for col, key in (("IP", "host"), ("Model", "model"), ("Serial", "serial"), ("MAC", "mac"),
                             ("Link", "link"), ("Role", "role"), ("Switch port", "port"), ("Notes", "notes"),
                             ("Firmware", "firmware")):
                if d[col]:
                    c[key] = d[col]
            if (d["Address"] or "static").lower() == "dhcp":
                c["address"] = "dhcp"
            if typ in TYPE_CHECKS and d["IP"]:
                c["checks"] = list(TYPE_CHECKS[typ])
            if d["Install RSSI"]:
                try:
                    c["install_rssi"] = float(d["Install RSSI"])
                except ValueError:
                    report.append(f"! {g['Group']} / {d['Name']}: Install RSSI '{d['Install RSSI']}' is not a number")
            entry["components"].append(c)
            comp_rows.append((entry, c, d))
        if kind == "vehicle" and g["Mobius asset"]:
            for typ, nm in (("vcu", "VCU"), ("gnss", "GNSS")) + ((("vai", "VAI"),) if g["Has VAI"].lower() == "yes" else ()):
                entry["components"].append({"id": unique(f"{gid}-{typ}"), "type": typ, "name": nm})
        if not entry["components"]:
            report.append(f"- {g['Group']}: no monitored devices yet - left out")
            continue
        tabs[TAB_OF_KIND.get(kind, "infrastructure")].append(entry)

    _wire_backhaul(tabs["infrastructure"], comp_rows, report, backhaul)
    data = {
        "site": {"id": slug(site), "name": name or meta.get("Name") or site.title(),
                 "timezone": timezone or meta.get("Time zone") or DEFAULT_TZ, "lat": lat, "lon": lon},
        "defaults": {"backhaul": backhaul, "types": TYPE_DEFAULTS},
        **({"mobius": {"influx_url": influx}} if influx else {}),
        "control_room": tabs["control_room"], "infrastructure": tabs["infrastructure"], "vehicles": tabs["vehicles"],
    }
    n_comp = sum(len(g["components"]) for t in tabs.values() for g in t)
    report.insert(0, f"{len(tabs['infrastructure'])} locations, {len(tabs['vehicles'])} vehicles, "
                     f"{len(tabs['control_room'])} control-room stations, {n_comp} components "
                     f"({skipped} spare/turned-off devices not monitored)")
    return data, report


def _position(lat: str, lon: str) -> tuple[float, float] | None:
    try:
        la, lo = float(lat), float(lon)
    except ValueError:
        return None
    return (round(la, 6), round(lo, 6)) if -90 <= la <= 90 and -180 <= lo <= 180 else None


def _infra(sheet: Sheet, site: str) -> str:
    return (sheet.site(site) or {}).get("Infrastructure subnet", "")


def _num(text, default: float, what: str, report: list[str]) -> float:
    if text in (None, ""):
        return default
    try:
        return float(text)
    except ValueError:
        report.append(f"! Sites sheet: {what} '{text}' is not a number - using {default}")
        return default


def _wire_backhaul(locations: list[dict], comp_rows, report: list[str], mode: str = "redundant") -> None:
    """How a trailer / the COW is reached.

    redundant (Mendon): management runs over the Tachyon links AND the Rajant mesh, so no
      trailer depends on another. Everything on a trailer hangs off its switch (it powers the
      radios over PoE); the switch itself has no upstream - either backhaul reaches it.
    tachyon: the 60 GHz links are the only path. The trailer's slave Tachyon is its uplink and
      hangs off the master at the far end; switch -> uplink; everything else -> switch."""
    by_link: dict[str, list[dict]] = {}
    for _g, c, _d in comp_rows:
        if c.get("link"):
            by_link.setdefault(c["link"], []).append(c)
    for loc in locations:
        comps = loc["components"]
        uplink = next((c for c in comps if c["type"] == "tachyon" and c.get("role") == "slave"), None)
        switch = next((c for c in comps if c["type"] == "switch"), None)
        if mode == "redundant":
            if uplink:
                uplink["uplink"] = True
            if switch:
                for c in comps:
                    if c is not switch and "depends_on" not in c:
                        c["depends_on"] = switch["id"]
            continue
        if uplink:
            uplink["uplink"] = True
            partner = next((x for x in by_link.get(uplink.get("link", ""), []) if x is not uplink), None)
            if partner:
                uplink["depends_on"] = partner["id"]
            else:
                report.append(f"- {loc['name']}: uplink link {uplink.get('link')} has no far end in the sheet")
        if switch and uplink:
            switch["depends_on"] = uplink["id"]
        hub = switch or uplink
        if hub:
            for c in comps:
                if c is not hub and c is not uplink and "depends_on" not in c:
                    c["depends_on"] = hub["id"]


def inventory_yaml(data: dict, source: str) -> str:
    import yaml
    head = (f"# Network Guru inventory - generated {dt.datetime.now():%Y-%m-%d %H:%M} from {source}\n"
            "# Edit the spreadsheet and run build-inventory again rather than editing this file.\n"
            "# No passwords here: *_ref names are looked up in the server's secrets.env.\n\n")
    return head + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120)
