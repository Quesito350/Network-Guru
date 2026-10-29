"""Live mode's first real driver: ping and TCP port checks (networkguru/drivers/reach.py)."""

import asyncio
import socket
import struct
import time

import pytest

from networkguru.drivers import NOT_MAPPED, reach
from networkguru.drivers.reach import (NOT_CHECKED, PINGS, ReachPoller, checksum, echo_request, icmp_available,
                                       parse_echo_reply, ports_of, reach_pollers, tcp_check)
from networkguru.events import EventStore
from networkguru.inventory import load_inventory, parse_inventory
from networkguru.model import Status
from networkguru.pipeline import Pipeline
from networkguru.rules import RuleSet, load_rules
from networkguru.tsdb import MemoryTSDB

from .conftest import EXAMPLE


def inv_of(*devices, tab="infrastructure"):
    return parse_inventory({"site": {"id": "lab", "name": "Lab", "timezone": "America/Denver"},
                            tab: [{"id": "g", "name": "G", "kind": "tower", "devices": list(devices)}]})


def test_icmp_packets():
    pkt = echo_request(0x1234, 7)
    assert pkt[0] == 8 and checksum(pkt) == 0                         # a valid checksum sums to zero
    reply = b"\x00\x00" + pkt[2:4] + struct.pack("!HH", 0x1234, 7) + pkt[8:]
    assert parse_echo_reply(reply) == 7
    ip_header = bytes([0x45]) + b"\0" * 19
    assert parse_echo_reply(ip_header + reply) == 7                    # with an IPv4 header in front
    assert parse_echo_reply(pkt) is None and parse_echo_reply(b"\0\0") is None


def test_ports_to_check():
    c = inv_of({"id": "m", "type": "mobius_server", "host": "10.9.1.5", "checks": ["tcp:18080", "udp:1", "tcp:x"]},
               {"id": "s", "type": "starlink", "target": "10.9.1.6:9200"}).comps
    assert ports_of(c["m"]) == [("10.9.1.5", 18080)] and ports_of(c["s"]) == [("10.9.1.6", 9200)]


def _poller(dev, can_ping=True):
    return ReachPoller(inv_of(dev).comps[dev["id"]], can_ping)


def test_results_from_ping_and_ports():
    p = _poller({"id": "r", "type": "rajant", "host": "10.9.1.7"})
    r = p.result(1.0, [], [2.0, 4.0, 6.0])                             # 3 of 4 replies
    m = {x.key: x.value for x in r.metrics}
    assert r.reachable and m == {"ping_rtt_ms": 4.0, "ping_loss_pct": 25.0}
    assert not p.result(1.0, [], []).reachable and p.result(1.0, [], []).error == "no reply to ping"

    gb = _poller({"id": "b", "type": "gnss_base", "host": "10.9.1.8", "checks": ["tcp:2101"]})
    r = gb.result(1.0, [("open", 12.3)], [])                            # ICMP filtered, the port answers
    m = {x.key: x.value for x in r.metrics}
    assert r.reachable and m == {"tcp_ms": 12.3, "port_up{port=2101}": 1.0}   # no 100 % loss shown
    r = gb.result(1.0, [("refused", 1.0)], [])                          # the device answered: port closed
    assert r.reachable and {x.key: x.value for x in r.metrics} == {"port_up{port=2101}": 0.0}
    r = gb.result(1.0, [("timeout", 3000.0)], [])
    assert not r.reachable and r.error == "no reply to ping; port 2101 timeout"

    ms = _poller({"id": "m", "type": "mobius_server", "host": "10.9.1.5", "checks": ["tcp:18080", "tcp:443"]})
    r = ms.result(1.0, [("refused", 1.0), ("open", 5.0)], [1.0, 1.0, 1.0, 1.0])
    m = {x.key: x.value for x in r.metrics}
    assert m["mobius_up"] == 0.0 and m["port_up{port=443}"] == 1.0 and m["tcp_ms{port=443}"] == 5.0   # its service


def test_tcp_check_open_closed_and_timeout():
    async def flow():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        opened = await tcp_check("127.0.0.1", port)
        server.close()
        await server.wait_closed()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
        s.close()
        closed = await tcp_check("127.0.0.1", free, timeout=5)
        dark = await tcp_check("192.0.2.1", 80, timeout=0.3)           # TEST-NET: nothing answers
        return opened, closed, dark
    opened, closed, dark = asyncio.run(flow())
    assert opened[0] == "open" and closed[0] == "refused" and dark[0] in ("timeout", "error")


def test_which_components_get_checked():
    inv = load_inventory(EXAMPLE)
    pollers, unchecked = reach_pollers(inv, can_ping=True)
    polled = {p.comp_ids[0] for p in pollers}
    assert "core-starlink" in polled and "mobius-svc" in polled and "t46-bc" in polled       # target / checks / host
    for cid, c in inv.comps.items():
        if c.type in ("vcu", "gnss", "vai", "mobius_ntrip"):
            assert unchecked[cid] == NOT_CHECKED and cid not in polled
    pollers, unchecked = reach_pollers(inv, can_ping=False)               # ping not permitted on this host
    assert {p.comp_ids[0] for p in pollers} == {"core-starlink", "t47-starlink", "mobius-svc", "mobius-influx", "gnss-base", "netmaker-api"}
    assert "ping is not permitted" in unchecked["t46-bc"]


def test_ping_here():
    ok, why = icmp_available()
    if not ok:
        pytest.skip(why)
    assert asyncio.run(reach.ping("127.0.0.1"))                          # this PC answers itself
    assert asyncio.run(reach.ping("192.0.2.1", count=1)) == []            # TEST-NET never answers


def test_live_pipeline_ok_then_offline(monkeypatch):
    """Real checks into the real pipeline: a device that answers is OK; one that never does goes
    Offline after 3 polls (Critical on the infrastructure tab)."""
    ok, _ = icmp_available()

    async def flow():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        inv = inv_of({"id": "up", "type": "host", "host": "127.0.0.1", "checks": [f"tcp:{port}"]},
                     {"id": "dark", "type": "host", "host": "192.0.2.1", "checks": ["tcp:80"]})
        monkeypatch.setattr(reach, "TCP_TIMEOUT_S", 0.3)
        monkeypatch.setattr(reach, "PING_WAIT_S", 0.3)
        pipe = Pipeline(inv, RuleSet(load_rules(None)), MemoryTSDB(), EventStore())
        pollers, _ = reach_pollers(inv, ok)
        now = time.time()
        for i in range(3):
            results = [r for p in pollers for r in await p.poll(now + i * 10)]
            pipe.ingest(results, now + i * 10)
        server.close()
        await server.wait_closed()
        return pipe.snapshot()["nodes"]
    nodes = asyncio.run(flow())
    assert nodes["up"]["s"] == int(Status.OK) and "tcp_ms" in nodes["up"]["m"]
    assert nodes["dark"]["s"] == int(Status.CRITICAL) and "Not responding" in nodes["dark"]["r"][0]


def test_live_runtime_note_and_unchecked(tmp_path):
    from fastapi.testclient import TestClient

    from networkguru.config import Settings
    from networkguru.web.app import create_app
    app = create_app(Settings(inventory=EXAMPLE, mode="live", history_days=1, vm_url=None, data_dir=tmp_path))
    with TestClient(app) as c:
        meta = c.get("/api/meta").json()
        # 0.9.4: what is checked is information (the footer's "What's checked"), the missing logins a banner
        assert any(n.startswith("Live mode: ping and port checks for every device") for n in meta["info"])
        assert not any(n.startswith("Live mode") for n in meta["notes"])
        assert "No VictoriaMetrics configured - history is kept in memory only." in meta["info"]
        assert any("RAJANT_SNMP_USER" in n for n in meta["notes"])             # readers not set up: named once
        live = c.get("/api/live").json()["nodes"]
        assert live["ac2845-vcu"]["r"] == [NOT_MAPPED] and len(app.state.rt.scheduler.pollers) > 20
    assert PINGS == 4
