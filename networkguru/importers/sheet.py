"""devices.xlsx - the one spreadsheet a site's inventory is kept in.

Sheets: Sites (name, time zone, location, subnets), Groups (vehicles, trailers, towers, stations),
Devices, Read me. No password columns:
logins are named once per device type in inventory defaults and typed on the server.
Importers add or update rows; Casey's edits are kept (rows are matched by serial, then IP,
then group + name, and an import only fills or refreshes the facts it owns).
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..catalog import TYPES

SITE_COLS = ["Site", "Name", "Time zone", "Lat", "Lon", "Mobius InfluxDB URL", "Subnets", "Infrastructure subnet",
             "Notes"]
GROUP_COLS = ["Site", "Group", "Kind", "Mobius asset", "Access", "Expected online", "Has VAI", "Lat", "Lon", "Notes"]
DEVICE_COLS = ["Site", "Group", "Type", "Name", "Model", "Serial", "MAC", "Address", "IP", "Link", "Role",
               "Install RSSI", "Switch port", "Firmware", "Monitor", "Notes"]
KINDS = ["vehicle", "trailer", "cow", "tower", "building", "repeater", "core", "server", "client"]
LISTS = {"Kind": KINDS, "Type": sorted(TYPES), "Address": ["static", "dhcp"], "Role": ["master", "slave"],
         "Monitor": ["yes", "no"], "Has VAI": ["yes", "no"], "Access": ["rajant", "celona"]}
README = [
    "Network Guru - site devices",
    "",
    "Sites: one row per site - the name shown in the dashboard, time zone, GPS (for the solar figures), the",
    "  Mobius InfluxDB URL, Subnets (comma-separated, e.g. 10.200.0.0/16: a Rajant found there belongs to this",
    "  site) and the Infrastructure subnet (an unnamed Rajant there is a tower, not a vehicle).",
    "Groups: one row per vehicle, trailer, tower, building, repeater, COW, network core, Mobius server or desk.",
    "  Lat / Lon: where its pin goes on the map (decimal degrees, e.g. 40.7602 / -111.8912). The Rajant snapshot",
    "  import fills them from the radios' GPS when empty; type or correct them here. Vehicles need none (they move).",
    "Devices: one row per device. Group must match a Groups row. Type picks how it is checked.",
    "Address: static, or dhcp (then give a Serial or MAC so it can be found when its IP changes).",
    "Link / Role / Install RSSI: 60 GHz Tachyon pairs share a Link name; master is the end toward the COW.",
    "Monitor = no: spare or turned-off devices (kept here, not checked).",
    "Vehicles with a Mobius asset name get VCU and GNSS checks (and VAI when Has VAI = yes) automatically.",
    "",
    "Never put passwords, SNMP communities, keys or tokens in this file.",
    "Rebuild the inventory after editing:  python run.py build-inventory devices.xlsx --site <site>",
]


@dataclass
class Sheet:
    sites: list[dict] = field(default_factory=list)
    groups: list[dict] = field(default_factory=list)
    devices: list[dict] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    # -- lookups / upserts -----------------------------------------------
    def site(self, site: str) -> dict | None:
        return next((r for r in self.sites if _eq(r["Site"], site)), None)

    def site_of(self, address: str) -> str | None:
        """The site whose Subnets contain this address, or None."""
        return next((r["Site"] for r in self.sites if _in_nets(address, r["Subnets"])), None)

    def group(self, site: str, name: str) -> dict | None:
        return next((g for g in self.groups if _eq(g["Site"], site) and _eq(g["Group"], name)), None)

    def upsert_group(self, site: str, name: str, kind: str, **extra) -> dict:
        g = self.group(site, name)
        if g is None:
            g = {c: "" for c in GROUP_COLS}
            g.update({"Site": site, "Group": name, "Kind": kind})
            self.groups.append(g)
            self.log.append(f"+ group {site}/{name} ({kind})")
        for k, v in extra.items():
            if v not in (None, "") and not g.get(k):
                g[k] = v
        return g

    def find_device(self, site: str, serial: str = "", ip: str = "", group: str = "", name: str = "") -> dict | None:
        if serial:
            d = next((d for d in self.devices if d["Serial"] and d["Serial"] == serial), None)
            if d:
                return d
        if ip:
            d = next((d for d in self.devices if _eq(d["Site"], site) and d["IP"] == ip), None)
            if d:
                return d
        if group and name:
            return next((d for d in self.devices if _eq(d["Site"], site) and _eq(d["Group"], group)
                         and _eq(d["Name"], name)), None)
        return None

    def upsert_device(self, row: dict, owned: tuple[str, ...] = ()) -> dict:
        """Add a device, or update one that matches. `owned` columns are refreshed from the
        import (e.g. IP, Firmware); other columns are only filled when empty."""
        d = self.find_device(row.get("Site", ""), row.get("Serial", ""), row.get("IP", ""),
                             row.get("Group", ""), row.get("Name", ""))
        if d is None:
            d = {c: "" for c in DEVICE_COLS}
            d.update({k: _cell(v) for k, v in row.items() if k in DEVICE_COLS})
            d["Monitor"] = d["Monitor"] or "yes"
            d["Address"] = d["Address"] or "static"
            self.devices.append(d)
            self.log.append(f"+ device {d['Site']}/{d['Group']}/{d['Name']} {d['IP']}")
            return d
        changed = []
        for k, v in row.items():
            if k not in DEVICE_COLS or v in (None, ""):
                continue
            v = _cell(v)
            if (k in owned and d[k] != v) or not d[k]:
                if d[k] != v:
                    changed.append(f"{k} {d[k] or '-'} -> {v}")
                d[k] = v
        if changed:
            self.log.append(f"~ device {d['Site']}/{d['Group']}/{d['Name']}: " + "; ".join(changed))
        return d

    # -- file I/O ---------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "Sheet":
        p = Path(path)
        if not p.exists():
            return cls()
        import openpyxl
        wb = openpyxl.load_workbook(p, data_only=True)
        try:
            return cls(sites=_read(wb, "Sites", SITE_COLS), groups=_read(wb, "Groups", GROUP_COLS),
                       devices=_read(wb, "Devices", DEVICE_COLS))
        finally:
            wb.close()

    def save(self, path: str | Path) -> None:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.worksheet.datavalidation import DataValidation
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        head_font = Font(bold=True, color="FFFFFF")
        head_fill = PatternFill("solid", fgColor="032C44")
        for title, cols, rows in (("Sites", SITE_COLS, sorted(self.sites, key=lambda r: r["Site"].lower())),
                                  ("Groups", GROUP_COLS, self._sorted_groups()),
                                  ("Devices", DEVICE_COLS, self._sorted_devices())):
            ws = wb.create_sheet(title)
            ws.append(cols)
            for c in ws[1]:
                c.font, c.fill = head_font, head_fill
            for r in rows:
                ws.append([r.get(c, "") for c in cols])
            ws.freeze_panes = "A2"
            for i, c in enumerate(cols, start=1):
                width = max([len(c)] + [len(str(r.get(c, ""))) for r in rows]) + 2
                ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = min(48, width)
            for c, values in LISTS.items():
                if c in cols:
                    letter = openpyxl.utils.get_column_letter(cols.index(c) + 1)
                    dv = DataValidation(type="list", formula1='"' + ",".join(values) + '"', allow_blank=True)
                    dv.add(f"{letter}2:{letter}2000")
                    ws.add_data_validation(dv)
        ws = wb.create_sheet("Read me")
        for line in README:
            ws.append([line])
        ws.column_dimensions["A"].width = 110
        ws["A1"].font = Font(bold=True, size=14)
        for row in ws.iter_rows():
            row[0].alignment = Alignment(wrap_text=False)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        wb.save(p)

    def _sorted_groups(self):
        order = {k: i for i, k in enumerate(KINDS)}
        return sorted(self.groups, key=lambda g: (g["Site"].lower(), order.get(g["Kind"], 99), _natural(g["Group"])))

    def _sorted_devices(self):
        gorder = {(g["Site"].lower(), g["Group"].lower()): i for i, g in enumerate(self._sorted_groups())}
        return sorted(self.devices, key=lambda d: (gorder.get((d["Site"].lower(), d["Group"].lower()), 9999),
                                                   _ip_key(d["IP"]), d["Name"]))


def read_devices_csv(path: str | Path) -> list[dict]:
    """The V&V radio-monitor devices.csv: name, site, type, ip, serial, model, firmware
    (+ optional group, kind, mobius_asset, address, mac)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def _read(wb, title: str, cols: list[str]) -> list[dict]:
    if title not in wb.sheetnames:
        return []
    ws = wb[title]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    head = [str(h or "").strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not any(v not in (None, "") for v in r):
            continue
        d = {c: "" for c in cols}
        for i, h in enumerate(head):
            if h in d and i < len(r):
                d[h] = _cell(r[i])
        out.append(d)
    return out


def _in_nets(address: str, nets: str) -> bool:
    """Is the address inside one of the comma-separated networks (bad entries are skipped)?"""
    import ipaddress
    try:
        ip = ipaddress.ip_address((address or "").strip())
    except ValueError:
        return False
    for n in (nets or "").replace(";", ",").split(","):
        try:
            if n.strip() and ip in ipaddress.ip_network(n.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _eq(a: str, b: str) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def _natural(s: str):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s or "")]


def _ip_key(ip: str):
    try:
        return tuple(int(x) for x in ip.split("."))
    except ValueError:
        return (999,)
