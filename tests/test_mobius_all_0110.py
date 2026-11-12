"""0.11.0 all of a vehicle's Mobius telemetry: every table (not the first 60), the first pass looking back a
day, each field's last value kept (a parked vehicle keeps it), the per-group cap, and one value's history for
its chart - only for a value the explorer has seen, nothing typed reaching the query. Made-up names only."""

import asyncio
import time

import pytest

from networkguru.drivers import mobius_explore as mx
from networkguru.drivers.mobius_explore import MobiusExplorer
from networkguru.inventory import parse_inventory

from .fake_devices import FakeInflux

TAG = "Vehicle"


def _inv(url):
    return parse_inventory({
        "site": {"id": "lab", "name": "Lab", "timezone": "America/Denver"},
        "mobius": {"influx_url": url, "database": "mobius", "asset_tag": TAG},
        "vehicles": [{"id": "truck-a", "name": "Truck A", "mobius_asset": "O'Neil Truck", "components": [
            {"id": "truck-a-bc", "type": "rajant", "host": "10.0.0.5"}]}],
        "control_room": [], "infrastructure": []})


def _tables(now):
    who = {TAG: "O'Neil Truck"}
    tables = {f"Telemetry {i:03d}": {"fields": {"Value": "float"}, "tags": {TAG: ["O'Neil Truck"]},
                                     "points": [(now - 30, who, {"Value": float(i)})]} for i in range(70)}
    tables["Old Table"] = {"fields": {"Value": "float"}, "tags": {TAG: ["O'Neil Truck"]},
                           "points": [(now - 5 * 3600, who, {"Value": 7.0})]}           # 5 h ago: the first pass only
    tables["Engine"] = {"fields": {"Rpm": "float", "Mode": "string", "api_key": "string"}, "tags": {TAG: ["O'Neil Truck"]},
                        "points": [(now - 60 * k, who, {"Rpm": 1000.0 + k, "Mode": "AUTO", "api_key": "leak"}) for k in range(1, 300)]}
    return tables


def test_every_table_the_day_then_ten_minutes_and_values_kept():
    now = time.time()
    fake = FakeInflux("mobius", _tables(now))
    try:
        ex = MobiusExplorer(_inv(fake.url), pause=0)
        asyncio.run(ex.once())
        first = [q for q in fake.queries if q.startswith("SELECT *")]
        rows = {(r["m"], r["f"]): r for r in ex.group("truck-a")["rows"]}
        assert len(ex.schema) == 72 and len(first) == 72                       # more than the old 60
        assert all("now() - 24h" in q for q in first)
        assert rows[("Old Table", "Value")]["v"] == 7.0 and rows[("Engine", "Rpm")]["v"] == 1001.0
        assert ("Engine", "api_key") not in rows                                 # secret-like fields stay out
        fake.queries.clear()
        asyncio.run(ex.once())
        again = [q for q in fake.queries if q.startswith("SELECT *")]
        assert again and all("now() - 10m" in q for q in again)
        kept = {(r["m"], r["f"]) for r in ex.group("truck-a")["rows"]}
        assert ("Old Table", "Value") in kept                                    # not in the last 10 min, still kept
        g = ex.group("truck-a")
        assert g["measurements"] == 72 and g["tables_read"] == 72 and ex.report()["values"] == len(kept)
        assert all(not k.startswith("_") for r in g["rows"] for k in r)          # the query details stay inside
    finally:
        fake.close()


def test_old_values_go_after_a_week_and_the_cap(monkeypatch):
    now = time.time()
    fake = FakeInflux("mobius", _tables(now))
    try:
        monkeypatch.setattr(mx, "MAX_ROWS", 20)
        ex = MobiusExplorer(_inv(fake.url), pause=0)
        asyncio.run(ex.once())
        rows = ex.group("truck-a")["rows"]
        assert len(rows) == 20 and ("Old Table", "Value") not in {(r["m"], r["f"]) for r in rows}   # the newest 20
        ex._expire(now + 8 * 86400)
        assert ex.group("truck-a")["rows"] == [] and ex.summary()["groups"] == {}
    finally:
        fake.close()


def test_history_of_a_seen_value_only():
    now = time.time()
    fake = FakeInflux("mobius", _tables(now))
    try:
        ex = MobiusExplorer(_inv(fake.url), pause=0)
        asyncio.run(ex.once())
        fake.queries.clear()
        h = asyncio.run(ex.history("truck-a", "Engine", "Rpm", "6h"))
        q = [x for x in fake.queries if x.startswith("SELECT mean(")]
        assert len(q) == 1 and "\"Vehicle\" = 'O\\'Neil Truck'" in q[0] and "time > now() - 6h" in q[0]
        assert "GROUP BY time(1m)" in q[0]
        assert len(h["points"]) > 100 and all(isinstance(v, float) for _t, v in h["points"])
        asyncio.run(ex.history("truck-a", "Engine", "Rpm", "6h"))                 # cached: no second query
        assert len([x for x in fake.queries if x.startswith("SELECT mean(")]) == 1
        with pytest.raises(KeyError):
            asyncio.run(ex.history("truck-a", "Engine\"; DROP", "Rpm", "6h"))      # not a value it has seen
        with pytest.raises(ValueError):
            asyncio.run(ex.history("truck-a", "Engine", "Mode", "6h"))            # text has no chart
        with pytest.raises(ValueError):
            asyncio.run(ex.history("truck-a", "Engine", "Rpm", "2y"))
        assert len(asyncio.run(ex.history("truck-a", "Engine", "Rpm", "7d"))["points"]) <= mx.MAX_POINTS
    finally:
        fake.close()


def test_history_endpoint_without_an_explorer(tmp_path):
    from fastapi.testclient import TestClient

    from networkguru.config import Settings
    from networkguru.web.app import create_app

    from .conftest import EXAMPLE
    app = create_app(Settings(inventory=EXAMPLE, mode="sim", history_days=1, vm_url=None, data_dir=tmp_path))
    with TestClient(app) as c:
        gid = next(iter(c.app.state.rt.inv.groups))
        assert c.get(f"/api/mobius/{gid}/history", params={"m": "x", "f": "y"}).status_code == 404
        assert c.get("/api/mobius/nope/history", params={"m": "x", "f": "y"}).status_code == 404
