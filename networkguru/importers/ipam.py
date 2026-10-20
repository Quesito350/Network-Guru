"""Read a site IPAM workbook laid out like this:

  IP | Location | Device | Switch Port | Cable Color | MM PTP SSID | MM PTP M/S | CH | RSSI | Notes

Reading starts at the header row; anything above it (that sheet keeps device logins there)
is skipped and never kept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
HEADERS = {"ip": "ip", "location": "location", "device": "device", "switch port": "port", "cable color": "cable",
           "mm ptp ssid": "ssid", "mm ptp m/s": "ms", "ch": "channel", "rssi": "rssi", "notes": "notes"}


@dataclass
class IpamRow:
    row: int
    ip: str
    location: str = ""
    device: str = ""
    port: str = ""
    cable: str = ""
    ssid: str = ""
    ms: str = ""
    channel: str = ""
    rssi: float | None = None
    notes: str = ""


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def read_ipam(path: str | Path) -> list[IpamRow]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        cols: dict[str, int] | None = None
        out: list[IpamRow] = []
        for rn, row in enumerate(ws.iter_rows(values_only=True), start=1):
            if cols is None:
                first = _s(row[0] if row else None).lower()
                names = [_s(v).lower() for v in row]
                if first == "ip" and "location" in names:          # header found - start here
                    cols = {HEADERS[n]: i for i, n in enumerate(names) if n in HEADERS}
                continue                                            # rows above the header: skipped
            ip = _s(row[cols["ip"]]) if cols.get("ip") is not None and cols["ip"] < len(row) else ""
            if not IPV4.match(ip):
                continue

            def get(key):
                i = cols.get(key)
                return _s(row[i]) if i is not None and i < len(row) else ""
            rssi = get("rssi")
            try:
                rssi_v = float(rssi) if rssi else None
            except ValueError:
                rssi_v = None
            out.append(IpamRow(rn, ip, get("location"), get("device"), get("port"), get("cable"), get("ssid"),
                               get("ms"), get("channel"), rssi_v, get("notes")))
        if cols is None:
            raise ValueError("no header row starting with 'IP' and containing 'Location' was found")
        return out
    finally:
        wb.close()


def device_type(text: str, location: str = "") -> str:
    t = f"{text} {location}".lower()
    if "switch" in t:
        return "switch"
    if "cerbo" in t or "victron" in t:
        return "victron_cerbo"
    if "rajant" in t or "breadcrumb" in t:
        return "rajant"
    if "tach" in t:                       # Tachyon / Tachion
        return "tachyon"
    if "starlink" in t:
        return "starlink"
    if "celona" in t:
        return "celona_ap"
    return "host"


def device_name(text: str, typ: str) -> str:
    name = re.sub(r"(?i)tachion", "Tachyon", text or "").replace("(future)", "").strip()
    name = re.sub(r"(?i)^switch mgmt$", "Switch", name)
    if typ == "host" and not name:
        return "Gateway"
    return name or typ


def location_group(location: str, device: str) -> tuple[str, str]:
    """(group name, kind) for an IPAM location."""
    loc = (location or "").strip()
    low = loc.lower()
    if not loc or low == "gateway" or (device or "").lower() == "gateway":
        return "Site network", "core"
    if "cow" in low:
        return "COW (Slipstream)", "cow"
    if "trailer" in low:
        return loc, "trailer"
    if "repeater" in low:
        return loc, "repeater"
    if "building" in low:
        return loc, "building"
    return loc, "tower"


def is_spare(row: IpamRow) -> bool:
    t = f"{row.device} {row.notes}".lower()
    return "future" in t or "turned off" in t
