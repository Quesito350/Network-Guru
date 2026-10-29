"""0.9.3: the Mobius database found by itself (an InfluxDB can have several), every Mobius vehicle's last
position for the map (kept across restarts), and the vehicle positions kept out of problem reports.
Against a fake InfluxDB that wants a database, as the real one does. Names and positions are made up."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from networkguru.drivers import live_pollers
from networkguru.drivers.influx import InfluxClient, InfluxError, find_database, forget_database
from networkguru.drivers.mobius import MobiusPoller
from networkguru.drivers.mobius_map import map_tables, normalise
from networkguru.drivers.readers import reader_for
from networkguru.importers.build import build_inventory
from networkguru.importers.sheet import Sheet
from networkguru.inventory import parse_inventory
from networkguru.positions import VehiclePositions
from networkguru.problem_report import _no_positions
from networkguru.web.api import map_vehicles

from .fake_devices import FakeInflux
from .test_mobius_map import MAP, _tables

DAY = 86400


def _inv(url, database=None, vehicles=None):
    mobius = {"influx_url": url, "asset_tag": "Vehicle", "map": MAP, **({"database": database} if database else {})}
    return parse_inventory({
        "site": {"id": "lab", "name": "Lab", "timezone": "America/Denver"},
        "mobius": mobius,
        "control_room": [{"id": "mob", "name": "Mobius Server", "kind": "server", "devices": [
            {"id": "mob-svc", "type": "mobius_server", "host": "127.0.0.1"},
            {"id": "mob-db", "type": "influxdb", "host": "127.0.0.1"}]}],
        "vehicles": vehicles if vehicles is not None else [
            {"id": "ta", "name": "Truck A", "mobius_asset": "Truck A",
             "devices": [{"id": "ta-vcu", "type": "vcu"}, {"id": "ta-gnss", "type": "gnss"}]}]})


def _fleet_tables(now):
    """Truck A live; Truck C (not in the inventory) live; Truck D parked three days ago, link false."""
    t = _tables(now)
    t["Position"]["points"] += [(now - 20, {"Vehicle": "Truck C"}, {"Lat": 40.51, "Lon": -111.91}),
                                (now - 3 * DAY, {"Vehicle": "Truck D"}, {"Lat": 40.52, "Lon": -111.92}),
                                (now - 30, {"Vehicle": "Truck E"}, {"Lat": 0.0, "Lon": 0.0})]         # no fix: ignored
    t["Link State"]["points"].append((now - 3 * DAY + 60, {"Vehicle": "Truck D"}, {"Online": False}))
    return t


def _pick(fake, tables=(), database=None):
    async def go():
        async with InfluxClient(fake.url, database) as c:
            return await find_database(c, tables)
    return asyncio.run(go())


def test_find_database_picks_the_one_with_the_map_tables():
    now = time.time()
    tables = map_tables({typ: normalise(values) for typ, values in MAP.items()})
    fake = FakeInflux("archive", {"Unrelated": {"fields": {"Value": "float"}, "tags": {}, "points": []}},
                      others={"live": _tables(now)}, version="3.7.0")
    try:
        assert _pick(fake, tables, database="set") == "set" and not fake.queries      # the configured one wins
        assert _pick(fake, tables) == "live"                                            # not just the first one
        n = len(fake.queries)
        assert _pick(fake, tables) == "live" and len(fake.queries) == n                 # remembered
        forget_database(fake.url)
        with pytest.raises(InfluxError, match=r"none of the Mobius map's tables is in any database \(archive, live\)"):
            _pick(fake, {"Nowhere"})
        forget_database(fake.url)
    finally:
        fake.close()
    one = FakeInflux("mobius", {})
    try:
        assert _pick(one, tables) == "mobius"                                          # only one: that one
    finally:
        forget_database(one.url)
        one.close()


def test_readers_find_the_database_themselves():
    now = time.time()
    fake = FakeInflux("archive", {}, others={"live": _tables(now)}, version="3.7.0")
    try:
        inv = _inv(fake.url)                                       # no database in the inventory (the 0.9.2 bug)
        mp = next(p for p in live_pollers(inv, can_ping=False)[0] if isinstance(p, MobiusPoller))
        got = {r.comp_id: r for r in asyncio.run(mp.poll(now))}
        server, _ = asyncio.run(reader_for(inv.comps["mob-svc"], inv).read(now))
        age, _ = asyncio.run(reader_for(inv.comps["mob-db"], inv).read(now))
    finally:
        forget_database(fake.url)
        fake.close()
    assert got["ta-vcu"].reachable and got["ta-gnss"].info == {"lat": 40.5, "lon": -111.9}
    assert {m.key: m.value for m in server} == {"mobius_assets": 2.0, "mobius_clients": 2.0}
    assert age[0].key == "influx_write_age_s"
    asked = [(q, db) for q, db in zip(fake.queries, fake.dbs) if q != "SHOW DATABASES" and q != "SHOW MEASUREMENTS"]
    assert asked and all(db == "live" for _q, db in asked)


def test_every_vehicle_on_the_map_and_kept_across_a_restart(tmp_path):
    now = time.time()
    fake = FakeInflux("mobius", _fleet_tables(now), version="3.7.0")
    path = tmp_path / "state" / "positions.json"
    try:
        inv = _inv(fake.url, "mobius")
        mp = MobiusPoller(inv, [inv.comps["ta-vcu"], inv.comps["ta-gnss"]], positions=VehiclePositions(path))
        asyncio.run(mp.poll(now))
    finally:
        fake.close()
    first = [q for q in fake.queries if 'FROM "Position"' in q]
    assert first and "now() - 7d" in first[0]                         # parked vehicles on the first start
    assert all("now() - 7d" not in q for q in fake.queries if 'FROM "Position"' not in q)
    pub = {v["name"]: v for v in mp.positions.public(mp.vehicle_of, now)}
    assert set(pub) == {"Truck A", "Truck C", "Truck D"}               # Truck E: 0, 0 is no position
    assert pub["Truck A"]["group"] == "ta" and pub["Truck A"]["live"]
    assert pub["Truck C"]["group"] is None and pub["Truck C"]["live"]  # in Mobius, not in the inventory
    assert pub["Truck D"]["group"] is None and not pub["Truck D"]["live"]
    assert (pub["Truck D"]["lat"], pub["Truck D"]["lon"]) == (40.52, -111.92)
    mp.positions.save(force=True)
    again = VehiclePositions(path)                                     # a restart: the last positions are back
    assert {v["name"]: (v["lat"], v["lon"], v["t"]) for v in again.public(mp.vehicle_of, now)} == \
        {n: (v["lat"], v["lon"], v["t"]) for n, v in pub.items()}
    counts = again.counts(mp.vehicle_of, now)
    assert counts["with_position"] == 3 and counts["matched"] == 1 and "lat" not in str(counts)


def test_a_refused_week_falls_back_to_a_day():
    now = time.time()
    fake = FakeInflux("mobius", _fleet_tables(now), version="3.7.0",
                      refuse=lambda q: "query would exceed file limit of 432 parquet files" if "now() - 7d" in q else None)
    try:
        inv = _inv(fake.url, "mobius")
        mp = MobiusPoller(inv, [inv.comps["ta-gnss"]])
        got = asyncio.run(mp.poll(now))
    finally:
        fake.close()
    assert got[0].reachable
    names = {v["name"] for v in mp.positions.public(mp.vehicle_of, now)}
    assert names == {"Truck A", "Truck C"}                             # Truck D is 3 days old: not in one day
    pos = [q for q in fake.queries if 'FROM "Position"' in q]
    assert ["7d" in q for q in pos] == [True, False]


def test_the_map_alone_runs_the_reader_and_matches_by_name(tmp_path):
    now = time.time()
    fake = FakeInflux("mobius", _fleet_tables(now))
    try:
        inv = _inv(fake.url, "mobius", vehicles=[                    # no Mobius asset, no VCU / GNSS - a radio only
            {"id": "tc", "name": "Truck C", "devices": [{"id": "tc-r", "type": "rajant", "host": "10.200.4.30"}]}])
        pollers, _unchecked, _notes = live_pollers(inv, can_ping=False)
        mp = next(p for p in pollers if isinstance(p, MobiusPoller))
        assert mp.comp_ids == []
        assert asyncio.run(mp.poll(now)) == []
        rt = SimpleNamespace(fleet=mp)
        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rt=rt)))
        body = asyncio.run(map_vehicles(req))
    finally:
        fake.close()
    assert body["enabled"]
    by = {v["name"]: v for v in body["vehicles"]}
    assert by["Truck C"]["group"] == "tc" and by["Truck A"]["group"] is None
    assert not any(isinstance(p, MobiusPoller) for p in live_pollers(inv, can_ping=False, only={"tc-r"})[0])


def test_positions_store_ignores_old_and_bad_values(tmp_path):
    clock = [1000.0]
    vp = VehiclePositions(tmp_path / "p.json", clock=lambda: clock[0])
    vp.position("X", 40.0, -111.0, 900.0)
    vp.position("X", 41.0, -112.0, 800.0)                              # older: kept out
    vp.position("X", 95.0, -111.0, 950.0)                              # not on Earth: kept out
    vp.link("X", True, 900.0)
    vp.heard("X", 950.0)
    assert vp.public(lambda n: None) == [{"name": "X", "group": None, "lat": 40.0, "lon": -111.0, "t": 900.0, "live": True}]
    vp.save()
    assert not (tmp_path / "p.json").exists()                          # at most once a minute
    clock[0] += 61
    vp.save()
    assert (tmp_path / "p.json").exists()
    vp.link("X", False, 1001.0)
    assert not vp.public(lambda n: None, now=1002.0)[0]["live"]


def test_problem_report_status_has_no_positions():
    snap = {"version": 3, "nodes": {"ta-gnss": {"s": 0, "i": {"lat": 40.5, "lon": -111.9, "address": "10.200.4.31"}},
                                    "ta": {"s": 0, "counts": {"ok": 1}}}}
    out = _no_positions(snap)
    assert out["nodes"]["ta-gnss"]["i"] == {"address": "10.200.4.31"} and out["nodes"]["ta"] == {"s": 0, "counts": {"ok": 1}}
    assert snap["nodes"]["ta-gnss"]["i"]["lat"] == 40.5                 # the live snapshot is not changed


def test_sites_sheet_mobius_database_to_inventory(tmp_path):
    sheet = Sheet()
    sheet.sites.append({"Site": "lab", "Name": "Lab", "Time zone": "America/Denver", "Lat": "", "Lon": "",
                        "Mobius InfluxDB URL": "http://10.200.1.10:8086", "Mobius database": "mobius",
                        "Mobius vehicle tag": "Vehicle", "Netmaker URL": "", "Subnets": "", "Infrastructure subnet": "",
                        "Notes": ""})
    path = tmp_path / "devices.xlsx"
    sheet.save(path)
    data, _report = build_inventory(Sheet.load(path), "lab")
    assert data["mobius"]["database"] == "mobius" and data["mobius"]["influx_url"] == "http://10.200.1.10:8086"
    sheet.sites[0]["Mobius database"] = ""
    sheet.save(path)
    data, _report = build_inventory(Sheet.load(path), "lab")
    assert "database" not in data["mobius"]                            # empty: found by itself
