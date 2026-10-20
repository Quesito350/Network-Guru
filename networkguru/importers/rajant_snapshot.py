"""Read Rajant BreadCrumbs from a BC|Commander snapshot (.mesh = gzipped protobuf NetworkSnapshot).

ONLY these whitelisted fields are read (tag paths inside each node record, tag 6):
  1 serial · 2 firmware · 4 address · 13 snapshot status · 8.10.2 platform · 8.10.37 hardware model
  8.10.12.1 live IPv4 · 8.10.19 MAC list · 8.150.2.{2,3} live GPS
  8.105.2.1.1 name · 8.105.2.10.{1,2} manual location · 8.105.2.20.1 IPv4 mode (DHCP / MANUAL)
The snapshot also holds mesh keys, admin passphrases, SNMP/AP/SSH/SSL secrets and the PC's
environment - none of that is ever decoded, kept or written.
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from pathlib import Path

from .protowire import all_of, first, text

MAX_BYTES = 512 * 1024 * 1024


@dataclass
class RajantNode:
    serial: str
    name: str
    platform: str = ""
    hw_model: str = ""
    firmware: str = ""
    address: str = ""
    ip_mode: str = ""                 # DHCP | MANUAL | ...
    mac: str = ""                     # first MAC (eth0)
    macs: list[str] = field(default_factory=list)
    lat: float | None = None
    lon: float | None = None
    answered: bool = True             # False: BC|Commander timed out reading this node's state

    @property
    def model(self) -> str:
        """'FE1-2255X' from serial 'FE1-2255X-100001' (more specific than the platform)."""
        return self.serial.rsplit("-", 1)[0] if self.serial.count("-") >= 2 else self.platform


def nmea_to_deg(value: str | None) -> float | None:
    """'4045.6000N' -> 40.76, '11153.40W' -> -111.89"""
    m = re.fullmatch(r"\s*(\d+)(\d\d\.\d+)\s*([NSEW])\s*", value or "")
    if not m:
        return None
    deg = int(m.group(1)) + float(m.group(2)) / 60
    return -deg if m.group(3) in "SW" else deg


def read_snapshot(path: str | Path) -> list[RajantNode]:
    data = Path(path).read_bytes()
    with gzip.GzipFile(fileobj=__import__("io").BytesIO(data)) as gz:
        raw = gz.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("snapshot is unreasonably large")
    return parse_snapshot(raw)


def parse_snapshot(raw: bytes) -> list[RajantNode]:
    nodes = []
    for n in all_of(raw, 6):
        serial = text(first(n, 1))
        if not serial:
            continue
        macs = (text(first(n, 8, 10, 19)) or "").split()
        lat = nmea_to_deg(text(first(n, 8, 150, 2, 2))) or nmea_to_deg(text(first(n, 8, 105, 2, 10, 1)))
        lon = nmea_to_deg(text(first(n, 8, 150, 2, 3))) or nmea_to_deg(text(first(n, 8, 105, 2, 10, 2)))
        nodes.append(RajantNode(
            serial=serial,
            name=text(first(n, 8, 105, 2, 1, 1)) or serial,
            platform=text(first(n, 8, 10, 2)) or "",
            hw_model=text(first(n, 8, 10, 37)) or "",
            firmware=text(first(n, 2)) or "",
            address=text(first(n, 8, 10, 12, 1)) or text(first(n, 4)) or "",
            ip_mode=(text(first(n, 8, 105, 2, 20, 1)) or "").upper(),
            mac=macs[0] if macs else "",
            macs=macs,
            lat=lat, lon=lon,
            answered=first(n, 13) in (None, 1),
        ))
    return nodes
