"""The whole app in simulator mode with one day of history (in-memory store)."""

import time

import pytest
from fastapi.testclient import TestClient

from networkguru.config import Settings
from networkguru.web.app import create_app

from .conftest import EXAMPLE


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    app = create_app(Settings(inventory=EXAMPLE, mode="sim", history_days=1, vm_url=None,
                              data_dir=tmp_path_factory.mktemp("api")))
    with TestClient(app) as c:
        for _ in range(200):
            if c.get("/api/meta").json()["backfill"]["done"]:
                break
            time.sleep(0.1)
        yield c


def test_meta_inventory_live(client):
    m = client.get("/api/meta").json()
    assert m["mode"] == "sim" and m["tsdb"] == "memory" and m["backfill"]["done"]
    assert "ping_rtt_ms" in m["thresholds"] and "gnss_mode" in m["enum_rules"]
    assert m["notes"][0].startswith("Simulated data") and m["info"] == []        # the simulator stays a banner (0.9.4)
    inv = client.get("/api/inventory").json()
    assert [t["id"] for t in inv["tabs"]] == ["vehicles", "control", "infrastructure"]
    live = client.get("/api/live").json()
    assert {"autocar-2845", "trailer-48", "vehicles", "t48-gx"} <= set(live["nodes"])
    assert client.get("/api/live/t48-gx").json()["tab"] == "infrastructure"
    # the simulator's vehicles come with their GNSS info; the Mobius positions are live mode only (0.9.3)
    assert client.get("/api/map/vehicles").json() == {"enabled": False, "vehicles": []}


def test_history_endpoints(client):
    now = time.time()
    charts = client.get("/api/history/charts/t48-gx", params={"from": now - 86400, "to": now}).json()
    kinds = {c["key"]: c["kind"] for c in charts}
    assert kinds["soc"] == "line" and kinds["mppt"] == "state" and kinds["yield"] == "daily"
    soc = next(c for c in charts if c["key"] == "soc")
    assert len(soc["series"][0]["points"]) > 20 and soc["thresholds"]["power_soc_pct"]["low_crit"] == 20
    prim = client.get("/api/history/charts/ac2845-vcu", params={"from": now - 3600, "primary": "true"}).json()
    assert all(c["key"] in ("latency", "drop") for c in prim)
    sp = client.get("/api/history/sparklines", params={"ids": "trailer-48,autocar-2846", "from": now - 86400}).json()
    assert sp["trailer-48"]["metric"] == "power_soc_pct"
    st = client.get("/api/history/status", params={"ids": "trailer-48", "from": now - 86400}).json()
    assert st["trailer-48"][0][0] == pytest.approx(now - 86400, abs=5)
    av = client.get("/api/availability", params={"ids": "trailer-46,vehicles", "from": now - 86400}).json()
    assert 0 <= av["trailer-46"]["pct"] <= 100


def test_path_and_events(client):
    now = time.time()
    hops = client.get("/api/path/yard-tractor-t1").json()
    assert [h["key"] for h in hops] == ["server", "core", "access", "radio", "router", "vcu", "vai"]
    assert hops[2]["label"] == "Celona private LTE"
    hist = client.get("/api/path/autocar-2846", params={"from": now - 86400}).json()
    assert "availability" in hist[0] and "edge_p95" in hist[3]
    evs = client.get("/api/events", params={"from": now - 86400, "limit": 5}).json()
    assert len(evs) <= 5 and all("name" in e for e in evs)
    assert client.get("/api/events", params={"node": "nope"}).status_code == 404


def test_validation(client):
    now = time.time()
    assert client.get("/api/history/charts/nope").status_code == 404
    assert client.get("/api/availability", params={"ids": "trailer-46", "from": now - 40 * 86400}).status_code == 400
    assert client.get("/api/path/trailer-46").status_code == 404


def test_fault_injection_changes_status(client):
    r = client.post("/api/sim/fault", json={"target": "trailer-49", "kind": "outage", "minutes": 5})
    assert r.status_code == 200
    deadline = time.time() + 45            # 3 missed 10-second polls before it counts as down
    while time.time() < deadline:
        if client.get("/api/live/t49-bc").json()["s"] == 4:
            break
        time.sleep(0.5)
    assert client.get("/api/live/t49-bc").json()["s"] == 4                   # critical: infrastructure down
    assert client.post("/api/sim/fault", json={"target": "x", "kind": "outage"}).status_code == 404


def test_websocket_snapshot_then_delta(client):
    with client.websocket_connect("/ws/live") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and "trailer-48" in snap["nodes"]
        msg = ws.receive_json()
        assert msg["type"] in ("delta", "hb")


def test_security_headers_and_static(client):
    r = client.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/static/js/app.js").status_code == 200
