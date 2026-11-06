"""0.9.5: vehicles that move on the map - each vehicle's heading (saved) and its trail of the last two
minutes (memory only), the quick position read while the Map is open, and the runtime's loop for it.
Against the fake InfluxDB; positions are made up."""

import asyncio
import math
import time
from types import SimpleNamespace

from networkguru import runtime
from networkguru.drivers.mobius import FAST_WINDOW, MobiusPoller
from networkguru.positions import TRAIL_S, VehiclePositions

from .fake_devices import FakeInflux
from .test_mobius_093 import _fleet_tables, _inv

M_LAT = 1 / 110540                       # one metre north, in degrees


def m_lon(lat):                          # one metre east
    return 1 / (111320 * math.cos(math.radians(lat)))


def test_heading_and_a_two_minute_trail(tmp_path):
    clock = [10_000.0]
    path = tmp_path / "p.json"
    vp = VehiclePositions(path, clock=lambda: clock[0])
    lat, lon = 40.0, -111.0
    for i in range(21):                                          # 5 m north every 10 s, for 200 s
        vp.position("X", lat + 5 * i * M_LAT, lon, 9_800.0 + 10 * i)
    vp.heard("X", 10_000.0)
    v = vp.public(lambda n: None)[0]
    assert v["live"] and v["heading"] == 0.0                      # north
    assert len(v["trail"]) == int(TRAIL_S / 10) + 1 and v["trail"][-1] == [round(v["lat"], 7), v["lon"]]
    vp.position("X", v["lat"], lon + 10 * m_lon(lat), 10_010.0)    # 10 m east
    assert vp.public(lambda n: None)[0]["heading"] == 90.0
    vp.position("X", v["lat"] + 0.5 * M_LAT, lon + 10 * m_lon(lat), 10_020.0)    # GPS wander: no new heading
    assert vp.public(lambda n: None)[0]["heading"] == 90.0
    vp.position("X", v["lat"] - 0.02, lon, 10_030.0)              # 2 km away (GPS back after a gap): a new trail
    assert vp.public(lambda n: None)[0]["heading"] == 90.0 and len(vp.trails["X"]) == 1
    vp.link("X", False, 10_040.0)                                 # not talking to Mobius: parked, no trail
    assert vp.public(lambda n: None)[0]["trail"] == []
    vp.save(force=True)
    again = VehiclePositions(path, clock=lambda: clock[0])         # a restart: the heading is kept, the trail is not
    assert again.data["X"]["heading"] == 90.0 and "trail" not in again.data["X"] and not again.trails


def test_quick_read_only_once_seeded_and_only_the_position_table(tmp_path):
    now = time.time()
    tables = _fleet_tables(now)
    fake = FakeInflux("mobius", tables, version="3.7.0")
    try:
        inv = _inv(fake.url, "mobius")
        mp = MobiusPoller(inv, [inv.comps["ta-vcu"], inv.comps["ta-gnss"]], positions=VehiclePositions())
        assert asyncio.run(mp.poll_positions()) == 0 and fake.queries == []      # nothing before the first poll
        asyncio.run(mp.poll(now))
        n = len(fake.queries)
        t = time.time()
        tables["Position"]["points"].append((t, {"Vehicle": "Truck A"}, {"Lat": 40.5, "Lon": -111.9 + 20 * m_lon(40.5)}))
        assert asyncio.run(mp.poll_positions()) >= 1
        quick = fake.queries[n:]
    finally:
        fake.close()
    assert quick and all('FROM "Position"' in q and f"now() - {FAST_WINDOW}" in q for q in quick)
    a = {v["name"]: v for v in mp.positions.public(mp.vehicle_of)}["Truck A"]
    assert a["lon"] > -111.9 and a["heading"] == 90.0 and len(a["trail"]) == 2
    assert mp.last[("ta", "lon")][1] == a["lon"]                  # the GNSS card's position follows too


def test_watched_for_a_minute():
    mp = SimpleNamespace(watched_until=0.0, _now=time.time)
    assert not MobiusPoller.watched(mp, 1000.0)
    MobiusPoller.watch(mp, 1000.0)
    assert MobiusPoller.watched(mp, 1059.0) and not MobiusPoller.watched(mp, 1061.0)


def test_the_runtime_reads_positions_only_while_the_map_is_open(monkeypatch, caplog):
    monkeypatch.setattr(runtime, "TRACK_S", 0.01)
    monkeypatch.setattr(runtime, "TRACK_SLOW_S", 0.05)
    calls, state = [], {"watched": False, "fail": False}

    async def poll_positions():
        calls.append(time.time())
        if state["fail"]:
            raise OSError("influx down")
        return 1

    fleet = SimpleNamespace(watched=lambda: state["watched"], poll_positions=poll_positions)
    rt = SimpleNamespace(fleet=fleet)

    async def go():
        task = asyncio.create_task(runtime.Runtime._track_vehicles(rt))
        await asyncio.sleep(0.1)
        idle = len(calls)
        state["watched"] = True
        await asyncio.sleep(0.1)
        busy = len(calls)
        state["fail"] = True
        await asyncio.sleep(0.3)
        task.cancel()
        return idle, busy

    idle, busy = asyncio.run(go())
    assert idle == 0 and busy >= 3
    assert sum("map positions not read" in r.message for r in caplog.records) == 1    # logged once, then slower
