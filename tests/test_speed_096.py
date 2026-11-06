"""0.9.6: the server's event loop kept free - one shared TLS context for every HTTP client, the stall meter,
ping and port times left out when they were taken during a stall (no false latency alarms), and the
Admin finder for slow steps."""

import asyncio
import io
import logging
import socket
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from networkguru import httpclient
from networkguru.config import Settings
from networkguru.drivers import reach
from networkguru.drivers.reach import ReachPoller, tcp_check
from networkguru.inventory import parse_inventory
from networkguru.stalls import StallMeter
from networkguru.web.app import create_app

from .conftest import EXAMPLE

PW = "correct-horse-battery"
HDR = {"X-Requested-With": "networkguru"}
PKG = Path(__file__).resolve().parents[1] / "networkguru"


def test_one_tls_context_and_quick_clients():
    assert httpclient.ssl_context() is httpclient.ssl_context()
    httpclient.client()
    t = time.perf_counter()
    for _ in range(20):
        httpclient.client(timeout=5)
    assert time.perf_counter() - t < 0.5                 # was ~200 ms each: the bundle loaded every time


def test_no_module_builds_its_own_client():
    makers = [p.relative_to(PKG).as_posix() for p in PKG.rglob("*.py")
              if p.name != "httpclient.py" and "httpx.AsyncClient(" in p.read_text(encoding="utf-8")]
    assert makers == []


def test_the_meter_sees_a_blocked_loop():
    m = StallMeter()

    async def go():
        task = asyncio.create_task(m.run())
        await asyncio.sleep(0.3)
        t0 = time.perf_counter()
        time.sleep(0.4)                                   # a step that blocks everything
        during = m.blocked(t0, time.perf_counter())       # the meter has not woken yet: the stall counts already
        await asyncio.sleep(0.3)
        st = m.summary()
        task.cancel()
        return t0, during, st

    t0, during, st = asyncio.run(go())
    assert during >= 0.25
    assert st["running"] and st["stalls_250ms"] == 1 and 350 <= st["stall_max_ms"] <= 900
    assert m.blocked(t0, t0 + 2) >= 0.3 and m.blocked(t0 - 5, t0 - 1) == 0.0
    assert m.summary()["running"] is False                # stopped with its task


def test_port_and_ping_times_taken_during_a_stall_are_left_out(monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    try:
        monkeypatch.setattr(reach, "METER", type("Busy", (), {"blocked": lambda self, a, b: 1.0})())
        state, ms = asyncio.run(tcp_check("127.0.0.1", port))
    finally:
        srv.close()
    assert (state, ms) == ("open", None)
    inv = parse_inventory({"site": {"id": "lab", "name": "Lab", "timezone": "America/Denver"},
                           "infrastructure": [{"id": "t1", "name": "T1", "devices": [
                               {"id": "t1-sw", "type": "switch", "host": "10.200.1.5", "checks": [f"tcp:{port}"]}]}]})
    p = ReachPoller(inv.comps["t1-sw"], can_ping=True)
    r = p.result(0.0, [("open", None)], [None, 10.0, None, 12.0])
    got = {m.key: m.value for m in r.metrics}
    assert r.reachable and "tcp_ms" not in got and got["ping_rtt_ms"] == 11.0 and got["ping_loss_pct"] == 0.0
    r = p.result(0.0, [("open", 4.0)], [None, None, None, None])     # every reply timed in a stall: no time, no loss
    got = {m.key: m.value for m in r.metrics}
    assert r.reachable and "ping_rtt_ms" not in got and got["ping_loss_pct"] == 0.0 and got["tcp_ms"] == 4.0


def test_slow_steps_are_logged_while_finding(caplog):
    m = StallMeter()

    async def go():
        m.slow_steps(0.05)                                # 3 seconds
        await asyncio.sleep(0)
        time.sleep(0.35)                                  # a slow step
        await asyncio.sleep(0)
        on = asyncio.get_running_loop().get_debug()
        m._slow_off()
        return on, asyncio.get_running_loop().get_debug()

    with caplog.at_level(logging.WARNING):
        on, after = asyncio.run(go())
    assert on and not after and m.slow_until == 0.0
    assert any(r.name == "asyncio" and "took" in r.getMessage() for r in caplog.records)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    app = create_app(Settings(inventory=EXAMPLE, mode="sim", history_days=1, vm_url=None, deploy="docker",
                              data_dir=tmp_path_factory.mktemp("speed"), dev_admin_password=PW))
    with TestClient(app) as c:
        yield c


def test_health_admin_and_the_problem_report_show_the_stalls(client):
    h = client.get("/api/health").json()
    assert {"stall_max_ms", "stalls_250ms", "running"} <= set(h)
    assert client.post("/api/admin/slow-steps", json={"minutes": 5}, headers=HDR).status_code == 401
    client.cookies.clear()
    assert client.post("/api/admin/login", json={"password": PW, "name": "Sam"}, headers=HDR).status_code == 200
    assert "speed" in client.get("/api/admin/status").json()
    assert client.post("/api/admin/slow-steps", json={"minutes": 0}, headers=HDR).status_code == 400
    r = client.post("/api/admin/slow-steps", json={"minutes": 5}, headers=HDR)
    assert r.status_code == 200 and r.json()["slow_steps_until"] > time.time()
    rt = client.app.state.rt
    assert any(e["reason"] == "Finding slow steps for 5 minutes" for e in rt.events.system_events(0, time.time() + 1))
    from networkguru.stalls import METER
    METER.slow_until = time.time() - 1                    # the meter switches it off on its next tick
    z = client.post("/api/admin/problem-report", json={"note": "", "browser": {}}, headers=HDR)
    text = zipfile.ZipFile(io.BytesIO(z.content)).read("report.txt").decode("utf-8")
    assert "Event loop stalls (last 10 min):" in text and "Mobius clients (counts only, never names): not read" in text
