"""What the SNMP reader asks each device type for, and how the answers become our metrics.

Standard MIBs, which every SNMP device has:
    SNMPv2-MIB        sysDescr, sysUpTime, sysName              -> uptime_s, info
    IF-MIB            64-bit octet counters, oper status, type  -> if_rx_bps / if_tx_bps, switch_ports_up
    POWER-ETHERNET    pethMainPseConsumptionPower (W)           -> switch_poe_w
Vendor values (Rajant peers / temperature / input voltage, Tachyon RSSI / SNR / MCS / link) go in
VENDOR once the vendor MIB files are at hand - only OID numbers and our names, never MIB text.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..model import Metric
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


@dataclass(frozen=True)
class Profile:
    traffic: bool = False       # if_rx_bps / if_tx_bps
    ports: bool = False         # switch_ports_up
    poe: bool = False           # switch_poe_w


@dataclass(frozen=True)
class VendorOid:
    oid: str
    metric: str
    scale: float = 1.0


PROFILES = {
    "rajant": Profile(traffic=True),
    "tachyon": Profile(traffic=True),
    "switch": Profile(ports=True, poe=True),
}
VENDOR: dict[str, list[VendorOid]] = {"rajant": [], "tachyon": []}     # filled from the vendor MIB files


def _index(oid: str, base: str) -> str:
    return oid[len(base) + 1:]


async def read(client: SnmpClient, ctype: str, state: dict, now: float) -> tuple[list[Metric], dict]:
    """One read of a device: metrics and info. `state` keeps the counters between reads."""
    prof = PROFILES.get(ctype, Profile())
    sys_ = await client.get([SYS_DESCR, SYS_UPTIME, SYS_NAME])
    if not sys_:
        raise SnmpError("the device answered, but without the standard system values")
    metrics: list[Metric] = []
    info: dict = {}
    if isinstance(sys_.get(SYS_UPTIME), int):
        metrics.append(Metric("uptime_s", sys_[SYS_UPTIME] / 100.0))
    for oid, key in ((SYS_NAME, "hostname"), (SYS_DESCR, "description")):
        v = sys_.get(oid)
        if isinstance(v, bytes) and v:
            info[key] = v.decode("utf-8", "replace").strip()[:200]
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
    vendor = VENDOR.get(ctype) or []
    if vendor:
        got = await client.get([v.oid for v in vendor])
        for v in vendor:
            if isinstance(got.get(v.oid), int):
                metrics.append(Metric(v.metric, got[v.oid] * v.scale))
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
