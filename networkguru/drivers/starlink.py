"""Starlink: the dish's own status API, reached through the trailer router's NAT (`target: host:port`).

The dish answers gRPC-Web on 192.168.100.1:9201 (plain HTTP/1.1 - httpx, no gRPC library), so the
router's dst-nat must point at the dish's port 9201, not 9200 (native gRPC). One call:
Device/Handle with `get_status`, decoded with the small protobuf reader in importers/protowire.py.

The field numbers come from community copies of the dish's (unofficial) protocol, checked against
each other; firmware can change them. Unknown fields are ignored and a missing one only leaves a
chart empty. proto3 leaves out zero values: a missing drop rate is 0 %, a missing alert is off.
"""

from __future__ import annotations

import httpx

from ..importers import protowire as pw
from ..model import Metric

PATH = "/SpaceX.API.Device.Device/Handle"
GET_STATUS = pw.field(1004, b"")                 # Request.get_status = 1004 (an empty message)
HEADERS = {"Content-Type": "application/grpc-web+proto", "Accept": "application/grpc-web+proto",
           "X-Grpc-Web": "1"}                    # no Origin / Referer: the dish answers empty to a foreign one
DISH_STATUS = 2004                               # Response.dish_get_status
ALERTS = {1: "motors stuck", 2: "thermal shutdown", 3: "thermal throttle", 4: "unexpected location",
          5: "mast not near vertical", 6: "slow Ethernet", 7: "roaming", 8: "install pending", 9: "heating",
          10: "power supply thermal throttle", 11: "power save idle", 14: "telemetry stale", 16: "low motor current",
          17: "lower signal than predicted", 18: "slow Ethernet (100 Mb)", 19: "obstruction map reset",
          20: "water in the dish", 21: "water in the router", 22: "slow router port", 23: "no Ethernet link"}
NOTICES = {7, 8, 9, 11, 19}      # roaming, install pending, heating (snow melt), power save, map reset: not faults


def frame(payload: bytes) -> bytes:
    """A gRPC(-Web) data frame: flag 0, 4-byte big-endian length, the message."""
    return b"\0" + len(payload).to_bytes(4, "big") + payload


REQUEST = frame(GET_STATUS)


class StarlinkError(Exception):
    pass


def unframe(body: bytes) -> tuple[bytes | None, dict[str, str]]:
    """(the message, the trailers) of a gRPC-Web response body."""
    msg, trailers, i = None, {}, 0
    while i + 5 <= len(body):
        flag, n = body[i], int.from_bytes(body[i + 1:i + 5], "big")
        data = body[i + 5:i + 5 + n]
        i += 5 + n
        if flag & 0x80:                                        # trailers, as header lines
            for line in data.decode("latin-1").splitlines():
                k, _, v = line.partition(":")
                if k.strip():
                    trailers[k.strip().lower()] = v.strip()
        elif msg is None:
            msg = data
    return msg, trailers


def _f32(v) -> float | None:
    return pw.double(v) if isinstance(v, (bytes, bytearray)) else None


def decode_status(msg: bytes) -> tuple[list[Metric], dict]:
    """Metrics and info from a Response message holding dish_get_status."""
    try:
        st = pw.first(msg, DISH_STATUS)
        if not isinstance(st, (bytes, bytearray)):
            raise StarlinkError("the answer has no dish status (is the target the dish, not the Starlink router?)")
        fields: dict[int, object] = {}
        for num, _wt, val in pw.iter_fields(st):
            fields.setdefault(num, val)
        latency = _f32(fields.get(1009))
        drop = _f32(fields.get(1003)) or 0.0
        obst = _f32(pw.first(st, 1004, 1)) or 0.0
        raised = [n for n, _wt, v in pw.iter_fields(fields.get(1005) or b"") if v]
        alerts = [ALERTS.get(n, f"alert {n}") for n in raised if n not in NOTICES]
        notices = [ALERTS[n] for n in raised if n in NOTICES]
        outage = fields.get(1014)
    except pw.WireError as e:
        raise StarlinkError(f"could not read the dish's answer ({e})") from None
    up = latency is not None and latency > 0 and outage is None
    metrics = [Metric("starlink_state", 1.0 if up else 0.0),
               Metric("starlink_pop_drop_pct", round(100.0 * drop, 2)),
               Metric("starlink_obstructed_pct", round(100.0 * obst, 2)),
               Metric("starlink_down_bps", round(_f32(fields.get(1007)) or 0.0)),
               Metric("starlink_up_bps", round(_f32(fields.get(1008)) or 0.0)),
               Metric("starlink_alerts", float(len(alerts)))]
    if latency:
        metrics.append(Metric("starlink_pop_latency_ms", round(latency, 1)))
    uptime = pw.first(st, 2, 1)
    if isinstance(uptime, int):
        metrics.append(Metric("uptime_s", float(uptime)))
    info: dict = {"dish_alerts": ", ".join(alerts) if alerts else "none"}
    if notices:
        info["dish_notices"] = ", ".join(notices)
    for num, key in ((2, "hardware"), (3, "software")):
        t = pw.text(pw.first(st, 1, num))
        if t:
            info[f"dish_{key}"] = t[:80]
    if isinstance(fields.get(1016), int):
        info["dish_ethernet"] = f"{fields[1016]} Mbit/s"
    if outage is not None:
        cause = pw.first(outage, 1)
        info["dish_outage"] = f"outage (cause {cause})" if isinstance(cause, int) else "outage"
    if fields.get(1022):
        info["dish_snr"] = "persistently low"
    elif 1018 in fields:
        info["dish_snr"] = "above the noise floor" if fields[1018] else "below the noise floor"
    return metrics, info


async def get_status(target: str, timeout: float = 8.0) -> tuple[list[Metric], dict]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"http://{target}{PATH}", content=REQUEST, headers=HEADERS)
    except httpx.HTTPError as e:
        raise StarlinkError(f"no answer from {target} ({type(e).__name__}) - check the router's NAT to the "
                            "dish (192.168.100.1:9201)") from None
    if r.status_code != 200:
        raise StarlinkError(f"{target} answered HTTP {r.status_code} - is the NAT pointing at the dish's port 9201?")
    msg, trailers = unframe(r.content)
    status = trailers.get("grpc-status") or r.headers.get("grpc-status")
    if status not in (None, "0"):
        why = trailers.get("grpc-message") or r.headers.get("grpc-message") or ""
        raise StarlinkError(f"the dish refused the request (gRPC status {status}{': ' + why if why else ''})")
    if not msg:
        raise StarlinkError(f"{target} answered with nothing - the NAT must go to the dish's port 9201 "
                            "(gRPC-Web), and nothing may add a Referer header on the way")
    return decode_status(msg)


class StarlinkReader:
    via = "starlink"
    label = "dish API"

    def __init__(self, comp):
        self.comp = comp
        self.target = comp.target if comp.target and ":" in comp.target else f"{comp.target or comp.host}:9201"

    def missing(self) -> list[str]:
        return []

    async def read(self, now: float) -> tuple[list[Metric], dict]:
        from .readers import ReadError
        try:
            return await get_status(self.target)
        except StarlinkError as e:
            raise ReadError(str(e)) from None
