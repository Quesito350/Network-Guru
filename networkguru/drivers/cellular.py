"""Cell modems (type cell_modem, private Celona LTE or a public carrier): which reader a model gets,
and the SNMP reads for the ones that have SNMP.

    Sierra RV55 / RV50 / MP70 / LX (ALEOS)   SNMP, Sierra's MIB (enterprise 20542)
    Sierra XR80 / XR90 / XR60 (AirLink OS)   no SNMP (Sierra) - ALMS / AirVantage instead (alms.py)
    MikroTik (LtAP, Chateau, SXT ...)        SNMP, MIKROTIK-MIB LTE modem table (enterprise 14988)
    OpenWrt / GL.iNet                        SNMP: snmpd "extend ngcell" running deploy/openwrt/ng-cell.sh

Identity numbers (IMEI, IMSI, ICCID, ESN) are never asked for - only signal, network and model.
"""

from __future__ import annotations

import re

from ..model import Metric

KINDS = {"cell_sierra": "Sierra ALEOS", "alms": "Sierra ALMS", "cell_mikrotik": "MikroTik LTE",
         "cell_openwrt": "OpenWrt"}
READERS = {"sierra": "cell_sierra", "alms": "alms", "mikrotik": "cell_mikrotik", "openwrt": "cell_openwrt"}
UNKNOWN_MODEL = "Cell modem model not recognised - Model column: RV55, XR80, MikroTik or OpenWrt"


def kind(model: str | None, reader: str | None = None) -> str | None:
    """The reader for a cell modem: from an explicit `reader:` (sierra | alms | mikrotik | openwrt)
    or the Model text."""
    if reader:
        return READERS.get(str(reader).strip().lower())
    m = (model or "").lower()
    if re.search(r"\bxr\s?-?(60|80|90)\b", m):
        return "alms"
    if re.search(r"\b(rv\s?-?5\d\w*|mp\s?-?70|lx\s?-?(40|60)|gx\s?-?450|es\s?-?450|aleos)\b", m) or "sierra" in m:
        return "cell_sierra"
    if "mikrotik" in m or re.search(r"\b(ltap|chateau|sxt|lhg|knot|atl)\b", m):
        return "cell_mikrotik"
    if "openwrt" in m or re.search(r"\bgl[.\- ]?(inet|x|e|ar|mt|xe)", m):
        return "cell_openwrt"
    return None


def connected(text: str | None) -> float | None:
    """1 / 0 from a modem's network-state text, None when it does not say."""
    t = (text or "").lower()
    if not t:
        return None
    if re.search(r"\bnot\b|\bno\b|search|denied|disconnect|idle|fail|down", t):
        return 0.0
    if re.search(r"connect|ready|register|online|\bup\b|attached|\bhome\b|roaming", t):
        return 1.0
    return None


def number(v) -> float | None:
    """A number from an INTEGER or a text value ("12.5 dB", "-94")."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:\.\d+)?", v)
        return float(m.group()) if m else None
    return None


def _text(v) -> str | None:
    if isinstance(v, bytes):
        s = v.decode("utf-8", "replace").strip().strip("\0")
        return s[:120] or None
    return None if v is None else str(v)


# -- Sierra ALEOS (RV55 ...) - scalars under 20542.9.1.1 (OIDs from Sierra's published MIB) --------
SIERRA = "1.3.6.1.4.1.20542.9.1.1"
SIERRA_OIDS = {
    "1.261": "rssi", "2.10210": "rsrp", "2.10209": "rsrq", "2.10211": "sinr",
    "1.259": "state", "1.264": "service", "1.770": "operator", "2.773": "cell_id", "2.2056": "band",
    "1.266": "supply", "1.267": "temp", "1.4": "aleos", "9.7": "model",
}


async def sierra(client, state: dict, now: float) -> tuple[list[Metric], dict]:
    oids = {f"{SIERRA}.{k}.0": name for k, name in SIERRA_OIDS.items()}
    raw = await client.get(list(oids))
    got = {oids[o]: v for o, v in raw.items() if o in oids}
    if not got:
        return [], {"cell": "no Sierra values - check with probe-snmp --walk 1.3.6.1.4.1.20542.9.1.1"}
    metrics = _signal(got.get("rssi"), got.get("rsrp"), got.get("rsrq"), got.get("sinr"))
    c = connected(_text(got.get("state")))
    if c is not None:
        metrics.append(Metric("cell_connected", c))
    v = number(got.get("supply"))
    if v is not None and 0 < v < 60:
        metrics.append(Metric("cell_supply_v", v))
    t = number(got.get("temp"))
    if t is not None and -40 < t < 120:
        metrics.append(Metric("cell_temp_c", t))
    info = {key: _text(got[name]) for name, key in (
        ("state", "network_state"), ("service", "service"), ("operator", "operator"), ("cell_id", "cell_id"),
        ("band", "band"), ("aleos", "aleos_version"), ("model", "modem_model")) if _text(got.get(name))}
    return metrics, info


def _signal(rssi, rsrp, rsrq, sinr) -> list[Metric]:
    out = []
    for name, v, lo, hi in (("cell_rssi_dbm", rssi, -140, -10), ("cell_rsrp_dbm", rsrp, -160, -30),
                            ("cell_rsrq_db", rsrq, -40, 5), ("cell_sinr_db", sinr, -30, 60)):
        x = number(v)
        if x is not None and lo <= x <= hi:                # outside the range = "no signal" codes
            out.append(Metric(name, x))
    return out


# -- MikroTik RouterOS LTE (mtxrLTEModemTable, one row per LTE interface) ------------------------
MT_LTE = "1.3.6.1.4.1.14988.1.1.16.1.1"
MT_COLS = {2: "rssi", 3: "rsrq_old", 4: "rsrp", 6: "tech", 7: "sinr", 15: "band", 16: "uptime", 17: "reg",
           19: "model", 20: "firmware", 21: "cqi", 22: "nr_rsrp", 24: "nr_sinr", 25: "rsrq10"}   # never 11-13
MT_TECH = {-1: "unknown", 0: "GSM compact", 1: "GSM", 2: "UMTS", 3: "EDGE", 4: "HSDPA", 5: "HSUPA",
           6: "HSPA", 7: "LTE", 11: "5G SA", 13: "5G NSA"}


async def mikrotik(client, state: dict, now: float) -> tuple[list[Metric], dict]:
    index = None
    for col in (4, 2, 7):                 # which LTE interface(s) there are, from one signal column
        rows = await client.walk(f"{MT_LTE}.{col}", max_rows=2, bulk=2)    # small steps: never into IMEI / IMSI
        if rows:
            index = rows[0][0][len(f"{MT_LTE}.{col}") + 1:]
            break
    if index is None:
        return [], {"cell": "no LTE modem in MikroTik's SNMP (RouterOS: /interface lte, SNMP on)"}
    oids = {f"{MT_LTE}.{c}.{index}": name for c, name in MT_COLS.items()}
    got = {oids[o]: v for o, v in (await client.get(list(oids))).items() if o in oids}
    rsrq = number(got.get("rsrq10"))
    metrics = _signal(got.get("rssi"), got.get("rsrp"),
                      rsrq / 10.0 if rsrq is not None else got.get("rsrq_old"), got.get("sinr"))
    c = connected(_text(got.get("reg")))
    if c is not None:
        metrics.append(Metric("cell_connected", c))
    info: dict = {}
    if isinstance(got.get("tech"), int):
        info["technology"] = MT_TECH.get(got["tech"], str(got["tech"]))
    for name, key in (("reg", "registration"), ("band", "band"), ("model", "modem_model"), ("firmware", "modem_firmware")):
        if _text(got.get(name)):
            info[key] = _text(got[name])
    if isinstance(got.get("cqi"), int):
        info["cqi"] = str(got["cqi"])
    if isinstance(got.get("uptime"), int) and got["uptime"] > 0:
        info["session_up_for"] = f"{got['uptime'] // 360000} h {got['uptime'] // 6000 % 60} min"
    nr = [f"RSRP {got['nr_rsrp']} dBm" if isinstance(got.get("nr_rsrp"), int) and got["nr_rsrp"] < 0 else None,
          f"SINR {got['nr_sinr']} dB" if isinstance(got.get("nr_sinr"), int) and got.get("nr_rsrp") else None]
    if any(nr):
        info["5g_signal"] = ", ".join(p for p in nr if p)
    return metrics, info


# -- OpenWrt: NET-SNMP-EXTEND-MIB nsExtendOutputFull."ngcell" (deploy/openwrt/ng-cell.sh) --------
EXTEND_FULL = "1.3.6.1.4.1.8072.1.3.2.3.1.2"
EXTEND_NAME = "ngcell"


def extend_oid(name: str = EXTEND_NAME) -> str:
    return f"{EXTEND_FULL}.{len(name)}." + ".".join(str(b) for b in name.encode())


def parse_keyvalues(text: str) -> dict[str, str]:
    out = {}
    for tok in re.split(r"[\s,;]+", text or ""):
        k, sep, v = tok.partition("=")
        if sep and k and v and v not in ("--", "-", "null", "none"):
            out[k.strip().lower()] = v.strip()[:80]
    return out


async def openwrt(client, state: dict, now: float) -> tuple[list[Metric], dict]:
    got = await client.get([extend_oid()])
    text = _text(next(iter(got.values()), None))
    if not text:
        return [], {"cell": "the ng-cell script is not set up on this modem (see Connecting the devices)"}
    kv = parse_keyvalues(text)
    metrics = _signal(kv.get("rssi"), kv.get("rsrp"), kv.get("rsrq"), kv.get("sinr") or kv.get("snr"))
    c = connected(kv.get("state") or kv.get("registration"))
    if c is not None:
        metrics.append(Metric("cell_connected", c))
    info = {key: kv[name].replace("_", " ") for name, key in (("operator", "operator"), ("tech", "technology"),
                                                              ("band", "band"), ("state", "network_state"),
                                                              ("registration", "registration")) if kv.get(name)}
    if not metrics and not info:
        info["cell"] = "the ng-cell script answered without signal values: " + text[:80]
    return metrics, info
