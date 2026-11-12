"""Every SNMP value of a device, on demand (0.11.0): a device page's "All SNMP values".

The reader (snmp_profiles.py) asks for a chosen few values every poll; this reads what the device offers
under these subtrees, once, when someone asks:

    System       1.3.6.1.2.1.1          sysDescr, sysName, sysUpTime, ...
    Interfaces   1.3.6.1.2.1.2          the interface table
                 1.3.6.1.2.1.31.1.1     its 64-bit counters, names and aliases
    PoE          1.3.6.1.2.1.105        POWER-ETHERNET-MIB
    Sensors      1.3.6.1.2.1.99         ENTITY-SENSOR-MIB
    Hardware     1.3.6.1.2.1.47.1.1.1   the entity (physical) table
    Vendor       Rajant 34861, Tachyon 57344 - their own subtree

At most MAX_VALUES values and MAX_S seconds (what was read by then is kept, marked cut short). Rajants,
Tachyons and switches only: a cell modem's MIBs hold SIM numbers (IMEI, IMSI, ICCID) and those are never
read. Any text that still looks like one (14-20 digits) is hidden, and every value goes through the secret
mask. The SNMP community and v3 user tables are outside these subtrees. Names come from a small built-in
table of the common standard OIDs and the vendor OIDs the dashboard knows - only numbers and our names,
never MIB text; the rest show as their number.
"""

from __future__ import annotations

import asyncio
import re
import time

from ..config import mask
from .snmp import END_OF_MIB, GETBULK, GETNEXT, IPADDR, MISSING, OCTETS, OID, TIMETICKS, SnmpError

TYPES = ("rajant", "tachyon", "switch")
STANDARD = [("System", "1.3.6.1.2.1.1"), ("Interfaces", "1.3.6.1.2.1.2"), ("Interfaces", "1.3.6.1.2.1.31.1.1"),
            ("PoE", "1.3.6.1.2.1.105"), ("Sensors", "1.3.6.1.2.1.99"), ("Hardware", "1.3.6.1.2.1.47.1.1.1")]
VENDOR = {"rajant": ("Vendor", "1.3.6.1.4.1.34861"), "tachyon": ("Vendor", "1.3.6.1.4.1.57344")}
MAX_VALUES = 5000
MAX_S = 90.0
KEEP_S = 300.0                 # a read is shown (not repeated) for this long
AGAIN_S = 30.0                 # one address starts a read at most this often
BULK = 25
MAX_TEXT = 300
SIM_LIKE = re.compile(r"^\+?\d{14,20}$")
HIDDEN = "(hidden: looks like a SIM or device number)"

_SYS = "1.3.6.1.2.1.1."
_IF = "1.3.6.1.2.1.2.2.1."
_IFX = "1.3.6.1.2.1.31.1.1.1."
_PSE_PORT = "1.3.6.1.2.1.105.1.1.1."
_PSE_MAIN = "1.3.6.1.2.1.105.1.3.1.1."
_SENSOR = "1.3.6.1.2.1.99.1.1.1."
_ENT = "1.3.6.1.2.1.47.1.1.1.1."


def _cols(prefix: str, names: list[str], start: int = 1) -> dict[str, str]:
    return {f"{prefix}{i}": n for i, n in enumerate(names, start) if n}


NAMES: dict[str, str] = {
    "1.3.6.1.2.1.2.1": "ifNumber",
    **_cols(_SYS, ["sysDescr", "sysObjectID", "sysUpTime", "sysContact", "sysName", "sysLocation", "sysServices"]),
    **_cols(_IF, ["ifIndex", "ifDescr", "ifType", "ifMtu", "ifSpeed", "ifPhysAddress", "ifAdminStatus", "ifOperStatus",
                  "ifLastChange", "ifInOctets", "ifInUcastPkts", "ifInNUcastPkts", "ifInDiscards", "ifInErrors",
                  "ifInUnknownProtos", "ifOutOctets", "ifOutUcastPkts", "ifOutNUcastPkts", "ifOutDiscards", "ifOutErrors",
                  "ifOutQLen", "ifSpecific"]),
    **_cols(_IFX, ["ifName", "ifInMulticastPkts", "ifInBroadcastPkts", "ifOutMulticastPkts", "ifOutBroadcastPkts",
                   "ifHCInOctets", "ifHCInUcastPkts", "ifHCInMulticastPkts", "ifHCInBroadcastPkts", "ifHCOutOctets",
                   "ifHCOutUcastPkts", "ifHCOutMulticastPkts", "ifHCOutBroadcastPkts", "ifLinkUpDownTrapEnable",
                   "ifHighSpeed", "ifPromiscuousMode", "ifConnectorPresent", "ifAlias", "ifCounterDiscontinuityTime"]),
    **_cols(_PSE_PORT, ["", "", "pethPsePortAdminEnable", "pethPsePortPowerPairsControlAbility", "pethPsePortPowerPairs",
                        "pethPsePortDetectionStatus", "pethPsePortPowerPriority", "pethPsePortMPSAbsentCounter",
                        "pethPsePortType", "pethPsePortPowerClassifications", "pethPsePortInvalidSignatureCounter",
                        "pethPsePortPowerDeniedCounter", "pethPsePortOverLoadCounter", "pethPsePortShortCounter"]),
    **_cols(_PSE_MAIN, ["", "pethMainPsePower", "pethMainPseOperStatus", "pethMainPseConsumptionPower",
                        "pethMainPseUsageThreshold"]),
    **_cols(_SENSOR, ["entPhySensorType", "entPhySensorScale", "entPhySensorPrecision", "entPhySensorValue",
                      "entPhySensorOperStatus", "entPhySensorUnitsDisplay", "entPhySensorValueTimeStamp",
                      "entPhySensorValueUpdateRate"]),
    **_cols(_ENT, ["", "entPhysicalDescr", "entPhysicalVendorType", "entPhysicalContainedIn", "entPhysicalClass",
                   "entPhysicalParentRelPos", "entPhysicalName", "entPhysicalHardwareRev", "entPhysicalFirmwareRev",
                   "entPhysicalSoftwareRev", "entPhysicalSerialNum", "entPhysicalMfgName", "entPhysicalModelName",
                   "entPhysicalAlias", "entPhysicalAssetID", "entPhysicalIsFRU"]),
}
IF_TABLES = (_IF, _IFX)

# the vendor OIDs the dashboard reads (snmp_profiles.py), relative to the vendor's base - our names
VENDOR_NAMES = {
    "rajant": [(r"^1\.1(\.0)?$", "serial number"), (r"^1\.2(\.0)?$", "temperature (0.01 °C)"),
               (r"^1\.3(\.0)?$", "free memory"), (r"^2\.7(\.0)?$", "dropped packets"),
               (r"^3\.(\d+)\.1(\.0)?$", "radio {0} name"), (r"^3\.(\d+)\.2(\.0)?$", "radio {0} frequency (MHz)"),
               (r"^3\.(\d+)\.3(\.0)?$", "radio {0} noise (dBm)"), (r"^3\.(\d+)\.4(\.0)?$", "radio {0} channel time counter"),
               (r"^3\.(\d+)\.5(\.0)?$", "radio {0} channel busy counter")],
    "tachyon": [(r"^1\.3\.1(\.0)?$", "model"), (r"^1\.3\.2(\.0)?$", "serial number"),
                (r"^1\.3\.3(\.0)?$", "CPU temperature (°C)"), (r"^1\.3\.4(\.0)?$", "firmware"),
                (r"^1\.3\.6(\.0)?$", "CPU (%)"), (r"^1\.3\.7(\.0)?$", "memory (%)"),
                (r"^1\.2\.5(\.0)?$", "peer count"),
                (r"^1\.2\.2\.1\.4\.(.+)$", "radio channel · {0}"), (r"^1\.2\.2\.1\.5\.(.+)$", "radio frequency (MHz) · {0}"),
                (r"^1\.2\.2\.1\.6\.(.+)$", "radio channel width (MHz) · {0}"),
                (r"^1\.2\.2\.1\.11\.(.+)$", "radio temperature (°C) · {0}"),
                (r"^1\.2\.6\.1\.6\.(.+)$", "peer signal (dBm) · {0}"), (r"^1\.2\.6\.1\.7\.(.+)$", "peer PHY rate · {0}"),
                (r"^1\.2\.6\.1\.8\.(.+)$", "peer SNR · {0}"), (r"^1\.2\.6\.1\.10\.(.+)$", "peer TX MCS · {0}"),
                (r"^1\.2\.6\.1\.12\.(.+)$", "peer link up (s) · {0}"), (r"^1\.2\.6\.1\.13\.(.+)$", "peer distance (m) · {0}"),
                (r"^1\.2\.6\.1\.18\.(.+)$", "peer model · {0}"), (r"^1\.2\.6\.1\.20\.(.+)$", "peer name · {0}"),
                (r"^1\.2\.6\.1\.26\.(.+)$", "peer firmware · {0}")],
}


def _duration(ticks: int) -> str:
    s = ticks // 100
    d, rest = divmod(s, 86400)
    h, rest = divmod(rest, 3600)
    return f"{d} d {h} h" if d else f"{h} h {rest // 60} min"


def show_value(tag: int, v) -> tuple[object, str]:
    """(value for the table, its kind). Text is decoded, MACs and odd bytes shown as hex, SIM-like text hidden."""
    if tag == TIMETICKS and isinstance(v, int):
        return f"{v} ({_duration(v)})", "time"
    if isinstance(v, bool) or v is None:
        return str(v), "text"
    if isinstance(v, int):
        return v, "number"
    if tag in (OID, IPADDR) or isinstance(v, str):
        text = str(v)
    elif isinstance(v, (bytes, bytearray)):
        b = bytes(v)
        try:
            text = b.decode("utf-8")
            if any(ord(ch) < 32 and ch not in "\t\r\n" for ch in text):
                raise ValueError
        except ValueError:
            text = ":".join(f"{x:02x}" for x in b) if len(b) == 6 else b[:64].hex(" ") + (" …" if len(b) > 64 else "")
    else:
        text = str(v)
    text = text.strip()
    if SIM_LIKE.match(text.replace(" ", "")):
        return HIDDEN, "hidden"
    return (mask(text) or "")[:MAX_TEXT], "text"


def name_of(oid: str, ctype: str, ifnames: dict[str, str]) -> str:
    """Our name for an OID: a known column plus its index (an interface's name), a vendor value, or ""."""
    best = ""
    for p in NAMES:
        if (oid == p or oid.startswith(p + ".")) and len(p) > len(best):
            best = p
    if best:
        rest = oid[len(best) + 1:]
        if any(best.startswith(t) for t in IF_TABLES) and rest in ifnames:
            return f"{NAMES[best]} · {ifnames[rest]}"
        return NAMES[best] if rest in ("", "0") else f"{NAMES[best]}.{rest}"
    base = VENDOR.get(ctype, ("", ""))[1]
    if base and oid.startswith(base + "."):
        sub = oid[len(base) + 1:]
        for pattern, label in VENDOR_NAMES.get(ctype, []):
            m = re.match(pattern, sub)
            if m:
                return label.format(*[g for g in m.groups() if g and not g.startswith(".")])
    return ""


async def walk_all(client, ctype: str, max_values: int = MAX_VALUES, max_s: float = MAX_S) -> dict:
    """Read every subtree for the type. The System subtree must answer (else SnmpError); a later
    subtree the device refuses is noted and skipped."""
    deadline = time.monotonic() + max_s
    raw: list[tuple[str, str, int, object]] = []          # (section, oid, tag, value)
    skipped: list[str] = []
    cut = False
    trees = STANDARD + ([VENDOR[ctype]] if ctype in VENDOR else [])
    for i, (section, base) in enumerate(trees):
        cur = base
        try:
            while True:
                if len(raw) >= max_values or time.monotonic() > deadline:
                    cut = True
                    break
                if client.version == "1":
                    binds = await client.request(GETNEXT, [cur])
                else:
                    binds = await client.request(GETBULK, [cur], 0, BULK)
                if not binds:
                    break
                done = False
                for oid, tag, v in binds:
                    if tag == END_OF_MIB or not oid.startswith(base + ".") or oid == cur:
                        done = True
                        break
                    if len(raw) >= max_values:
                        cut = done = True
                        break
                    cur = oid
                    if tag not in MISSING:
                        raw.append((section, oid, tag, v))
                if done:
                    break
        except SnmpError as e:
            if i == 0:
                raise
            skipped.append(f"{section} ({base}): {e}")
        if cut:
            break
    ifnames: dict[str, str] = {}
    for _s, oid, _t, v in raw:                              # ifName wins over ifDescr
        for prefix in (_IF + "2.", _IFX + "1."):
            if oid.startswith(prefix) and isinstance(v, (bytes, bytearray)):
                name = bytes(v).decode("utf-8", "replace").strip()
                if name and (prefix.startswith(_IFX) or oid[len(prefix):] not in ifnames):
                    ifnames[oid[len(prefix):]] = name[:60]
    rows = []
    for section, oid, tag, v in raw:
        value, kind = show_value(tag, v)
        rows.append({"s": section, "oid": oid, "name": name_of(oid, ctype, ifnames), "v": value, "k": kind})
    return {"rows": rows, "cut": cut, "skipped": skipped}


class SnmpWalks:
    """The latest full read per device; one read at a time on the server; an address starts one at most
    every AGAIN_S. States: running / done / error."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.results: dict[str, dict] = {}
        self.running: str | None = None
        self.by_ip: dict[str, float] = {}
        self.task: asyncio.Task | None = None

    def get(self, cid: str) -> dict:
        r = self.results.get(cid)
        return dict(r, keep_s=KEEP_S) if r else {"state": "none", "keep_s": KEEP_S}

    def start(self, comp, ip: str) -> dict:
        """Starts a read (or returns the running / recent one). ValueError: busy elsewhere, or too soon."""
        from .readers import SnmpReader
        now = self.clock()
        cur = self.results.get(comp.id)
        if self.running == comp.id:
            return self.get(comp.id)
        if cur and cur.get("state") == "done" and now - cur["t"] < KEEP_S:
            return self.get(comp.id)
        if self.running:
            raise ValueError("Another device's values are being read - try again in a minute.")
        if now - self.by_ip.get(ip, -1e18) < AGAIN_S:
            raise ValueError("You started a read a moment ago - try again in half a minute.")
        reader = SnmpReader(comp)
        missing = reader.missing()
        if missing:
            raise ValueError(f"{reader.label} is not set up - add {', '.join(missing)} to secrets.env")
        self.by_ip[ip] = now
        if len(self.by_ip) > 500:
            self.by_ip = {k: t for k, t in self.by_ip.items() if now - t < AGAIN_S}
        self.running = comp.id
        self.results[comp.id] = {"state": "running", "t": now, "label": reader.label}
        self.task = asyncio.get_running_loop().create_task(self._go(comp, reader))
        return self.get(comp.id)

    async def _go(self, comp, reader) -> None:
        t0 = time.monotonic()
        client = None
        try:
            client = reader._client_for()
            got = await walk_all(client, comp.type)
            self.results[comp.id] = {"state": "done", "t": self.clock(), "label": reader.label,
                                     "took_s": round(time.monotonic() - t0, 1), **got}
        except SnmpError as e:
            self.results[comp.id] = {"state": "error", "t": self.clock(), "label": reader.label,
                                     "error": f"{e}{reader.hint(str(e))}"}
        except Exception as e:  # noqa: BLE001 - a surprise from a device must not stay "running"
            self.results[comp.id] = {"state": "error", "t": self.clock(), "label": reader.label,
                                     "error": f"read failed ({type(e).__name__})"}
        finally:
            if client is not None:
                client.close()
            self.running = None
