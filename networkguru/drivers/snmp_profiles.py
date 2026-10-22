"""What the SNMP reader asks each device type for, and how the answers become our metrics.

Standard MIBs, which every SNMP device has:
    SNMPv2-MIB        sysDescr, sysUpTime, sysName              -> uptime_s, info
    IF-MIB            64-bit octet counters, oper status, type  -> if_rx_bps / if_tx_bps, switch_ports_up
    POWER-ETHERNET    pethMainPseConsumptionPower (W)           -> switch_poe_w
Vendor values, from the vendor MIB files (0.8.0) - only OID numbers and our names, never MIB text:
    Rajant  (enterprise 34861)       temperature, per-radio noise and channel busy, dropped packets
    Tachyon (enterprise 57344.1)     the 60 GHz peer: rx power, SNR, MCS, rate; radio temperature, CPU
    cell modems (cellular.py)        Sierra ALEOS, MikroTik LTE, OpenWrt (a small script via snmpd)
A vendor part that fails leaves the standard values in place (info.vendor_error says why).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..model import Metric
from . import cellular
from .snmp import SnmpClient, SnmpError

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
IF_TYPE = "1.3.6.1.2.1.2.2.1.3"
IF_OPER = "1.3.6.1.2.1.2.2.1.8"
IF_IN32 = "1.3.6.1.2.1.2.2.1.10"
IF_OUT32 = "1.3.6.1.2.1.2.2.1.16"
IF_HC_IN = "1.3.6.1.2.1.31.1.1.1.6"
IF_HC_OUT = "1.3.6.1.2.1.31.1.1.1.10"
PETH_MAIN_POWER = "1.3.6.1.2.1.105.1.3.1.1.4"
LOOPBACK = 24
PORT_TYPES = {6, 62, 69, 117}          # ethernetCsmacd, fastEther, fastEtherFX, gigabitEthernet

RAJANT = "1.3.6.1.4.1.34861"           # .1 system  .2 InstaMesh  .3.N radio N (wlan0 = 1)
TACHYON = "1.3.6.1.4.1.57344.1"        # .2 wireless  .3 system
T_RADIOS = TACHYON + ".2.2.1"          # table, indexed by the radio name
T_PEER_COUNT = TACHYON + ".2.5"
T_PEERS = TACHYON + ".2.6.1"           # table, indexed by the peer MAC


@dataclass(frozen=True)
class Profile:
    traffic: bool = False       # if_rx_bps / if_tx_bps
    ports: bool = False         # switch_ports_up
    poe: bool = False           # switch_poe_w


PROFILES = {
    "rajant": Profile(traffic=True),
    "tachyon": Profile(traffic=True),
    "switch": Profile(ports=True, poe=True),
}


def _index(oid: str, base: str) -> str:
    return oid[len(base) + 1:]


def _text(v) -> str | None:
    if isinstance(v, bytes):
        s = v.decode("utf-8", "replace").strip().strip("\0")
        return s[:200] or None
    return None if v is None else str(v)


def scalars(rows: list[tuple[str, object]], base: str) -> dict[str, object]:
    """{"1.2": value} from a walk under `base` - with or without the usual ".0" on each scalar
    (Rajant answers on the bare OIDs, the way its Zabbix template asks for them)."""
    out: dict[str, object] = {}
    for oid, v in rows:
        sub = _index(oid, base)
        if sub.endswith(".0"):
            sub = sub[:-2]
        out.setdefault(sub, v)
    return out


def table(rows: list[tuple[str, object]], base: str) -> dict[str, dict[int, object]]:
    """{row index: {column: value}} from a walk of a table's entry OID."""
    out: dict[str, dict[int, object]] = {}
    for oid, v in rows:
        col, _, idx = _index(oid, base).partition(".")
        if col.isdigit():
            out.setdefault(idx, {})[int(col)] = v
    return out


def rate(state: dict, key: str, value, now: float, wrap: int = 2 ** 64) -> float | None:
    """Per-second change of a counter between two reads (None on the first read or a reset)."""
    if not isinstance(value, int):
        return None
    prev = state.get(key)
    state[key] = (now, value)
    if not prev or now - prev[0] < 1:
        return None
    d = (value - prev[1]) % wrap
    if d > wrap // 2:
        return None
    return d / (now - prev[0])


def share(state: dict, key: str, part, whole, wrap: int = 2 ** 32) -> float | None:
    """100 * change of `part` / change of `whole` between two reads (e.g. channel busy time)."""
    if not isinstance(part, int) or not isinstance(whole, int):
        return None
    prev = state.get(key)
    state[key] = (part, whole)
    if not prev:
        return None
    dp, dw = (part - prev[0]) % wrap, (whole - prev[1]) % wrap
    if dw <= 0 or dp > wrap // 2 or dw > wrap // 2:
        return None
    return max(0.0, min(100.0, 100.0 * dp / dw))


async def read(client: SnmpClient, ctype: str, state: dict, now: float) -> tuple[list[Metric], dict]:
    """One read of a device: metrics and info. `state` keeps the counters between reads.
    `ctype` is the component type, or a cell modem's kind (cellular.KINDS)."""
    prof = PROFILES.get(ctype, Profile())
    sys_ = await client.get([SYS_DESCR, SYS_UPTIME, SYS_NAME])
    if not sys_:
        raise SnmpError("the device answered, but without the standard system values")
    metrics: list[Metric] = []
    info: dict = {}
    if isinstance(sys_.get(SYS_UPTIME), int):
        metrics.append(Metric("uptime_s", sys_[SYS_UPTIME] / 100.0))
    for oid, key in ((SYS_NAME, "hostname"), (SYS_DESCR, "description")):
        v = _text(sys_.get(oid))
        if v:
            info[key] = v
    if prof.traffic or prof.ports:
        types = {_index(o, IF_TYPE): v for o, v in await client.walk(IF_TYPE)}
        oper = {_index(o, IF_OPER): v for o, v in await client.walk(IF_OPER)}
        if prof.ports:
            metrics.append(Metric("switch_ports_up", float(sum(1 for i, t in types.items()
                                                              if t in PORT_TYPES and oper.get(i) == 1))))
        if prof.traffic:
            m = await _traffic(client, types, oper, state, now)
            metrics.extend(m)
    if prof.poe:
        poe = [v for _o, v in await client.walk(PETH_MAIN_POWER) if isinstance(v, int)]
        if poe:
            metrics.append(Metric("switch_poe_w", float(sum(poe))))
    vendor = VENDOR.get(ctype)
    if vendor:
        try:
            vm, vi = await vendor(client, state, now)
        except SnmpError as e:
            info["vendor_error"] = f"vendor values not read: {e}"
        else:
            metrics.extend(vm)
            info.update(vi)
    return metrics, info


async def _traffic(client: SnmpClient, types: dict, oper: dict, state: dict, now: float) -> list[Metric]:
    """Bits per second through the device's up, non-loopback interfaces, from two reads."""
    up = {i for i, t in types.items() if t != LOOPBACK and oper.get(i) == 1}
    rx = {_index(o, IF_HC_IN): v for o, v in await client.walk(IF_HC_IN)}
    tx = {_index(o, IF_HC_OUT): v for o, v in await client.walk(IF_HC_OUT)}
    wrap = 2 ** 64
    if not rx:                                               # no 64-bit counters: the 32-bit ones
        rx = {_index(o, IF_IN32): v for o, v in await client.walk(IF_IN32)}
        tx = {_index(o, IF_OUT32): v for o, v in await client.walk(IF_OUT32)}
        wrap = 2 ** 32
    cur = {i: (rx.get(i), tx.get(i)) for i in up if isinstance(rx.get(i), int) and isinstance(tx.get(i), int)}
    prev = state.get("if")
    state["if"] = (now, cur)
    if not prev or now - prev[0] < 1:
        return []
    dt = now - prev[0]
    drx = dtx = 0
    for i, (r, t) in cur.items():
        if i not in prev[1]:
            continue
        pr, pt = prev[1][i]
        dr, dtt = (r - pr) % wrap, (t - pt) % wrap
        if dr > wrap // 2 or dtt > wrap // 2:                 # a counter reset, not traffic
            continue
        drx, dtx = drx + dr, dtx + dtt
    return [Metric("if_rx_bps", drx * 8 / dt), Metric("if_tx_bps", dtx * 8 / dt)]


# -- Rajant BreadCrumb ----------------------------------------------------------------
async def _rajant(client: SnmpClient, state: dict, now: float) -> tuple[list[Metric], dict]:
    got = scalars(await client.walk(RAJANT, max_rows=150), RAJANT)
    metrics: list[Metric] = []
    info: dict = {}
    temp = got.get("1.2")
    if isinstance(temp, int):          # hundredths of a degree (Rajant's Zabbix template: x0.01)
        metrics.append(Metric("rajant_temp_c", round(temp / 100.0, 1)))
    if _text(got.get("1.1")):
        info["serial"] = _text(got["1.1"])
    if isinstance(got.get("1.3"), int):
        info["free_memory"] = f"{got['1.3']:,}"
    drops = rate(state, "rajant_drop", got.get("2.7"), now)
    if drops is not None:
        metrics.append(Metric("rajant_drop_pps", round(drops, 2)))
    noise, busy = [], []
    for n in range(1, 9):
        if not any(f"3.{n}.{col}" in got for col in (1, 2, 3, 4, 5)):
            continue
        name = _text(got.get(f"3.{n}.1")) or f"radio {n}"
        parts = []
        freq = got.get(f"3.{n}.2")
        if isinstance(freq, int) and freq > 0:
            parts.append(f"{freq} MHz")
        nz = got.get(f"3.{n}.3")
        if isinstance(nz, int) and -130 <= nz <= -20:
            noise.append(nz)
            parts.append(f"noise {nz} dBm")
        b = share(state, f"rajant_busy{n}", got.get(f"3.{n}.5"), got.get(f"3.{n}.4"))
        if b is not None:
            busy.append(b)
            parts.append(f"channel busy {b:.0f} %")
        info[name] = ", ".join(parts) or "no values"
    if noise:
        metrics.append(Metric("rajant_noise_dbm", float(max(noise))))        # the noisiest radio
    if busy:
        metrics.append(Metric("rajant_busy_pct", round(max(busy), 1)))      # the busiest radio
    return metrics, info


# -- Tachyon 60 GHz ---------------------------------------------------------------------
def _dbm(v: int) -> float:
    return v / 10.0 if abs(v) > 150 else float(v)          # tenths of a dB, if the firmware sends those


async def _tachyon(client: SnmpClient, state: dict, now: float) -> tuple[list[Metric], dict]:
    metrics: list[Metric] = []
    info: dict = {}
    sysx = scalars(await client.walk(TACHYON + ".3", max_rows=40), TACHYON + ".3")
    for sub, key in (("1", "model"), ("2", "serial"), ("4", "firmware")):
        if _text(sysx.get(sub)):
            info[key] = _text(sysx[sub])
    if isinstance(sysx.get("3"), int):
        info["cpu_temperature"] = f"{sysx['3']} °C"
    for sub, metric in (("6", "tachyon_cpu_pct"), ("7", "tachyon_mem_pct")):
        if isinstance(sysx.get(sub), int) and 0 <= sysx[sub] <= 100:
            metrics.append(Metric(metric, float(sysx[sub])))
    radios = table(await client.walk(T_RADIOS, max_rows=80), T_RADIOS)
    temps = [r[11] for r in radios.values() if isinstance(r.get(11), int) and -40 < r[11] < 150]
    if temps:
        metrics.append(Metric("tachyon_radio_temp_c", float(max(temps))))
    for r in list(radios.values())[:1]:
        parts = [f"channel {r[4]}" if isinstance(r.get(4), int) else None,
                 f"{r[5]} MHz" if isinstance(r.get(5), int) and r[5] > 0 else None,
                 f"{r[6]} MHz wide" if isinstance(r.get(6), int) and r[6] > 0 else None]
        if any(parts):
            info["radio"] = ", ".join(p for p in parts if p)
    peers = table(await client.walk(T_PEERS, max_rows=600), T_PEERS)
    count = await client.get([T_PEER_COUNT + ".0", T_PEER_COUNT])
    n = next((v for v in count.values() if isinstance(v, int)), len(peers))
    metrics.append(Metric("tachyon_link_up", 1.0 if n > 0 or peers else 0.0))
    heard = [p for p in peers.values() if isinstance(p.get(6), int)]
    if heard:
        p = min(heard, key=lambda r: r[6])                    # several peers: the weakest link
        metrics.append(Metric("tachyon_rssi_dbm", _dbm(p[6])))
        if isinstance(p.get(8), int):
            metrics.append(Metric("tachyon_snr_db", p[8] / 10.0 if abs(p[8]) > 100 else float(p[8])))
        if isinstance(p.get(10), int):
            metrics.append(Metric("tachyon_tx_mcs", float(p[10])))
        if isinstance(p.get(7), int):                        # Mbit/s (kbit/s if it is that large)
            metrics.append(Metric("tachyon_phy_rate_mbps", p[7] / 1000.0 if p[7] > 20000 else float(p[7])))
        for col, key in ((20, "peer"), (18, "peer_model"), (26, "peer_firmware")):
            if _text(p.get(col)):
                info[key] = _text(p[col])
        if "peer" not in info and _text(p.get(1)):
            info["peer"] = _text(p[1])
        if isinstance(p.get(13), int) and p[13] > 0:
            info["link_distance"] = f"{p[13]} m"
        if isinstance(p.get(12), int) and p[12] > 0:
            info["link_up_for"] = _duration(p[12])
        if len(heard) > 1:
            info["peers"] = str(len(heard))
    return metrics, info


def _duration(s: int) -> str:
    d, rest = divmod(int(s), 86400)
    h, rest = divmod(rest, 3600)
    return f"{d} d {h} h" if d else f"{h} h {rest // 60} min"


VENDOR = {
    "rajant": _rajant,
    "tachyon": _tachyon,
    "cell_sierra": cellular.sierra,
    "cell_mikrotik": cellular.mikrotik,
    "cell_openwrt": cellular.openwrt,
}
