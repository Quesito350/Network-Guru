"""Reachability: ping and TCP port checks, standard library only.

    ping         4 echo requests per poll -> ping_rtt_ms (average) and ping_loss_pct
                 Linux / the container: an unprivileged ICMP datagram socket (needs the
                 net.ipv4.ping_group_range sysctl, set in both compose files) - no root, no raw socket.
                 Windows (run.py serve on a PC): IcmpSendEcho from iphlpapi.dll, in a thread.
    ports        every `checks: ["tcp:PORT"]` entry and a `target: host:port` (e.g. Starlink's API
                 through the trailer router) -> tcp_ms (connect time) and port_up (1 open / 0 closed);
                 a Mobius server's port is mobius_up instead (its service).

A device is reachable when it answers a ping or any port (a refused port also proves the device is
there). When every ping is lost but a port answers, the ping numbers are left out (ICMP is probably
filtered on the way) instead of showing 100 % loss. The scheduler's 3-misses-then-Offline rule and
the parent re-check work as with the simulator.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import struct
import sys
import time

from ..catalog import type_spec
from ..model import CompResult, Metric

log = logging.getLogger("networkguru.drivers")

PINGS = 4                 # echo requests per poll
PING_GAP_S = 0.2          # between the requests
PING_WAIT_S = 1.0         # for the last reply
TCP_TIMEOUT_S = 3.0
PORT_METRIC = {"mobius_server": "mobius_up"}     # a port that IS the device's service
NOT_CHECKED = "Not checked yet - needs the Mobius reader (0.7.0)"


# -- ICMP ----------------------------------------------------------------------
def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def echo_request(ident: int, seq: int, payload: bytes = b"networkguru-ping") -> bytes:
    head = struct.pack("!BBHHH", 8, 0, 0, ident & 0xFFFF, seq & 0xFFFF)
    return struct.pack("!BBHHH", 8, 0, checksum(head + payload), ident & 0xFFFF, seq & 0xFFFF) + payload


def parse_echo_reply(data: bytes) -> int | None:
    """The sequence number of an ICMP echo reply, or None. A datagram ICMP socket gives the ICMP
    message without the IP header; a leading IPv4 header is skipped just in case."""
    if len(data) >= 20 and data[0] >> 4 == 4:
        data = data[(data[0] & 0x0F) * 4:]
    if len(data) < 8 or data[0] != 0:
        return None
    return struct.unpack("!H", data[6:8])[0]


def icmp_available() -> tuple[bool, str]:
    """Can this process ping without root? (False, why) when it cannot."""
    if sys.platform == "win32":
        try:
            _win_icmp()
            return True, ""
        except OSError as e:
            return False, f"ping is not available on this PC ({e})"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        s.close()
        return True, ""
    except OSError as e:
        return False, (f"ping is not permitted here ({e.strerror or e}) - allow it with the sysctl "
                       "net.ipv4.ping_group_range, as both compose files do; port checks still run")


async def ping(host: str, count: int = PINGS) -> list[float]:
    """Round-trip times in ms of the replies (an empty list: no reply at all)."""
    if sys.platform == "win32":
        return await asyncio.to_thread(_win_ping, host, count)
    return await _linux_ping(host, count)


async def _linux_ping(host: str, count: int) -> list[float]:
    loop = asyncio.get_running_loop()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    s.setblocking(False)
    sent: dict[int, float] = {}
    rtts: list[float] = []
    ident = os.getpid() & 0xFFFF            # the kernel puts its own id on a datagram ICMP socket
    try:
        async def receive():
            while len(rtts) < count:
                data = await loop.sock_recv(s, 1500)
                seq = parse_echo_reply(data)
                if seq in sent:
                    rtts.append((time.perf_counter() - sent.pop(seq)) * 1000)

        rx = asyncio.ensure_future(receive())
        try:
            for seq in range(1, count + 1):
                sent[seq] = time.perf_counter()
                await loop.sock_sendto(s, echo_request(ident, seq), (host, 0))
                if seq < count:
                    await asyncio.sleep(PING_GAP_S)
            await asyncio.wait_for(asyncio.shield(rx), PING_WAIT_S)
        except (TimeoutError, asyncio.TimeoutError, OSError):
            pass
        finally:
            rx.cancel()
    finally:
        s.close()
    return rtts


_WIN = None


def _win_icmp():
    """iphlpapi's IcmpSendEcho (no admin rights needed)."""
    global _WIN
    if _WIN is None:
        import ctypes
        from ctypes import wintypes

        class IP_OPTION_INFORMATION(ctypes.Structure):
            _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                        ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]

        class ICMP_ECHO_REPLY(ctypes.Structure):
            _fields_ = [("Address", wintypes.ULONG), ("Status", wintypes.ULONG), ("RoundTripTime", wintypes.ULONG),
                        ("DataSize", wintypes.USHORT), ("Reserved", wintypes.USHORT), ("Data", ctypes.c_void_p),
                        ("Options", IP_OPTION_INFORMATION)]

        lib = ctypes.WinDLL("iphlpapi.dll", use_last_error=True)
        lib.IcmpCreateFile.restype = wintypes.HANDLE
        lib.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
        lib.IcmpSendEcho.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.WORD,
                                     ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
        lib.IcmpSendEcho.restype = wintypes.DWORD
        _WIN = (ctypes, lib, ICMP_ECHO_REPLY)
    return _WIN


def _win_ping(host: str, count: int) -> list[float]:
    ctypes, lib, reply_t = _win_icmp()
    addr = int.from_bytes(socket.inet_aton(host), "little")      # IPAddr: network byte order in memory
    payload = b"networkguru-ping"
    size = ctypes.sizeof(reply_t) + len(payload) + 64
    handle = lib.IcmpCreateFile()
    rtts: list[float] = []
    try:
        for i in range(count):
            buf = ctypes.create_string_buffer(size)
            t0 = time.perf_counter()
            n = lib.IcmpSendEcho(handle, addr, payload, len(payload), None, buf, size, int(PING_WAIT_S * 1000))
            if n:
                rep = reply_t.from_buffer(buf)
                if rep.Status == 0:                              # IP_SUCCESS (0 ms: under a millisecond)
                    rtts.append(float(rep.RoundTripTime) or (time.perf_counter() - t0) * 1000)
            if i < count - 1:
                time.sleep(PING_GAP_S)
    finally:
        lib.IcmpCloseHandle(handle)
    return rtts


# -- TCP -----------------------------------------------------------------------
async def tcp_check(host: str, port: int, timeout: float = TCP_TIMEOUT_S) -> tuple[str, float]:
    """('open', ms) | ('refused', ms) | ('timeout', ms) | ('error', ms)."""
    t0 = time.perf_counter()
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except ConnectionRefusedError:
        return "refused", (time.perf_counter() - t0) * 1000
    except (TimeoutError, asyncio.TimeoutError):
        return "timeout", (time.perf_counter() - t0) * 1000
    except OSError:
        return "error", (time.perf_counter() - t0) * 1000
    ms = (time.perf_counter() - t0) * 1000
    w.close()
    try:
        await asyncio.wait_for(w.wait_closed(), 1.0)
    except (TimeoutError, asyncio.TimeoutError, OSError):
        pass
    return "open", ms


def ports_of(comp) -> list[tuple[str, int]]:
    """(host, port) pairs to check: `checks: ["tcp:PORT"]` on its host, and `target: host:port`."""
    out = []
    for chk in comp.checks or []:
        kind, _, port = str(chk).partition(":")
        if kind.strip().lower() == "tcp" and port.strip().isdigit() and comp.host:
            out.append((comp.host.split(":")[0], int(port)))
    if comp.target and ":" in comp.target:
        host, _, port = comp.target.rpartition(":")
        if port.isdigit():
            out.append((host, int(port)))
    return out


def _ipv4(host: str | None) -> str | None:
    try:
        ip = ipaddress.ip_address((host or "").split(":")[0])
    except ValueError:
        return None
    return str(ip) if ip.version == 4 else None


# -- the poller ----------------------------------------------------------------
class ReachPoller:
    """One component: ping its host (when its type is pingable) and check its ports."""

    def __init__(self, comp, can_ping: bool, interval: float | None = None):
        self.comp = comp
        self.comp_ids = [comp.id]
        self.interval = interval or type_spec(comp.type).interval
        self.can_ping = can_ping
        self.ping_host = _ipv4(comp.host) if can_ping and type_spec(comp.type).ping else None
        self.ports = ports_of(comp)
        self.timeout = PINGS * PING_GAP_S + PING_WAIT_S + TCP_TIMEOUT_S + 3.0

    def retarget(self, host: str) -> None:
        """The discovery scan found the device at another address: check it there from now on."""
        self.comp.host = host
        self.ping_host = _ipv4(host) if self.can_ping and type_spec(self.comp.type).ping else None
        self.ports = ports_of(self.comp)

    async def poll(self, now: float) -> list[CompResult]:
        jobs = [tcp_check(h, p) for h, p in self.ports]
        if self.ping_host:
            jobs.append(ping(self.ping_host))
        got = await asyncio.gather(*jobs, return_exceptions=True)
        port_res = got[:len(self.ports)]
        rtts = got[-1] if self.ping_host else None
        if isinstance(rtts, BaseException):
            log.warning("ping %s failed: %s", self.ping_host, type(rtts).__name__)
            rtts = []
        return [self.result(now, port_res, rtts)]

    def result(self, now: float, port_res: list, rtts: list[float] | None) -> CompResult:
        metrics: list[Metric] = []
        problems: list[str] = []
        answered = False
        several = len(self.ports) > 1
        for (host, port), res in zip(self.ports, port_res):
            state, ms = res if not isinstance(res, BaseException) else ("error", 0.0)
            labels = {"port": str(port)} if several else {}
            up_metric = PORT_METRIC.get(self.comp.type) if (host, port) == self.ports[0] else None
            if state == "open":
                answered = True
                metrics.append(Metric("tcp_ms", round(ms, 1), labels))
            else:
                problems.append(f"port {port} {'closed' if state == 'refused' else state}")
                answered = answered or state == "refused"            # the device itself answered
            metrics.append(Metric(up_metric or "port_up", 1.0 if state == "open" else 0.0,
                                  {} if up_metric else {"port": str(port)}))
        if rtts is not None:
            if rtts:
                answered = True
                metrics.append(Metric("ping_rtt_ms", round(sum(rtts) / len(rtts), 1)))
                metrics.append(Metric("ping_loss_pct", round(100.0 * (PINGS - len(rtts)) / PINGS, 1)))
            else:
                problems.insert(0, "no reply to ping")
        if not answered:
            return CompResult(self.comp.id, now, False, error="; ".join(problems) or "no answer")
        # the address it answered on - a change (DHCP, the discovery scan) becomes an "Address changed" event
        info = {"address": self.comp.host.split(":")[0]} if self.comp.host else {}
        return CompResult(self.comp.id, now, True, metrics=metrics, info=info)


def reach_pollers(inv, can_ping: bool) -> tuple[list[ReachPoller], dict[str, str]]:
    """Pollers for every component with something to check, and {comp_id: reason} for the rest."""
    pollers, unchecked = [], {}
    for c in inv.comps.values():
        p = ReachPoller(c, can_ping)
        if p.ping_host or p.ports:
            pollers.append(p)
        elif type_spec(c.type).source == "mobius":
            unchecked[c.id] = NOT_CHECKED
        elif not can_ping and _ipv4(c.host):
            unchecked[c.id] = "Not checked - ping is not permitted on this host, and it has no port to check"
    return pollers, unchecked
