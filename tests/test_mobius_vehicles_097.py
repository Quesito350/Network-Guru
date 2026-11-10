"""0.9.7: vehicles straight from Mobius - what each vehicle is (the map's `vehicle` values), the ones devices.xlsx
has not got becoming vehicles of their own (VCU, GNSS, their IP pinged), the ones it has linked by name or by
IP, the simulator's left out - and the Mobius clients' connections. Names, tables and addresses are made up."""

import asyncio
import time
from types import SimpleNamespace

import yaml

from networkguru.drivers.mobius import MobiusPoller, simulated
from networkguru.drivers.readers import MobiusServerReader, address_of, reader_for
from networkguru.found import load_site_inventory, merge_added
from networkguru.importers.build import build_inventory
from networkguru.importers.sheet import Sheet
from networkguru.inventory import parse_inventory
from networkguru.mobius_vehicles import KEEP_S, MobiusVehicles
from networkguru.positions import VehiclePositions
from networkguru.runtime import Runtime
from networkguru.web.api import mobius_clients, mobius_vehicles

from .fake_devices import FakeInflux

DAY = 86400
MAP = {"vcu": {"vcu_communicating": {"measurement": "Link State", "field": "Online"}},
       "gnss": {"lat": {"measurement": "Position", "field": "Lat"}, "lon": {"measurement": "Position", "field": "Lon"}},
       "vehicle": {"vehicle_ip": {"measurement": "Vai State", "field": "Ip"},
                   "vehicle_mac": {"measurement": "Id Mac"}, "vehicle_model": {"measurement": "Id Model"},
                   "vehicle_type": {"measurement": "Id Type"}}}
SITE = {"id": "lab", "name": "Lab", "timezone": "America/Denver"}


def _raw(url="http://127.0.0.1:9", vehicles=None):
    return {"site": SITE, "mobius": {"influx_url": url, "database": "mobius", "asset_tag": "Vehicle", "map": MAP},
            "vehicles": vehicles if vehicles is not None else [
                {"id": "roller", "name": "Roller", "components": [{"id": "roller-bc", "type": "rajant", "host": "10.200.4.94"}]},
                {"id": "truck-a", "name": "Truck A", "mobius_asset": "Truck A", "components": [
                    {"id": "truck-a-vcu", "type": "vcu"}, {"id": "truck-a-gnss", "type": "gnss"},
                    {"id": "truck-a-bc", "type": "rajant", "host": "10.200.4.10"}]}]}


def _tables(now):
    v = lambda n: {"Vehicle": n}                                                      # noqa: E731
    return {
        "Link State": {"fields": {"Online": "boolean"}, "tags": {}, "points": [(now - 5, v("Dozer9"), {"Online": True})]},
        "Position": {"fields": {"Lat": "float", "Lon": "float"}, "tags": {},
                     "points": [(now - 3, v("Dozer9"), {"Lat": 40.5, "Lon": -111.9})]},
        "Vai State": {"fields": {"Ip": "string"}, "tags": {}, "points": [
            (now - 60, v("Roller7"), {"Ip": "10.200.4.94"}), (now - 60, v("Dozer9"), {"Ip": "10.200.4.50"}),
            (now - 60, v("Excavator_9"), {"Ip": "127.0.0.1"}), (now - 60, v("Truck A"), {"Ip": "10.200.4.11"})]},
        "Id Mac": {"fields": {"Value": "string"}, "tags": {}, "points": [
            (now - 60, v("Dozer9"), {"Value": "02-00-00-00-00-09"}), (now - 60, v("Excavator_9"), {"Value": "0-0-0-0-0-0"}),
            (now - 60, v("Loader3"), {"Value": "unknown"})]},
        "Id Model": {"fields": {"Value": "string"}, "tags": {}, "points": [(now - 60, v("Dozer9"), {"Value": "D71"})]},
        "Id Type": {"fields": {"Value": "string"}, "tags": {}, "points": [(now - 60, v("Dozer9"), {"Value": "Bulldozer"})]},
    }


def test_the_reader_learns_what_each_vehicle_is():
    now = time.time()
    fake = FakeInflux("mobius", _tables(now), version="3.7.0")
    try:
        inv = parse_inventory(_raw(fake.url))
        mp = MobiusPoller(inv, [inv.comps["truck-a-vcu"], inv.comps["truck-a-gnss"]], positions=VehiclePositions())
        asyncio.run(mp.poll(now))
        n = len(fake.queries)
        asyncio.run(mp.poll(now))                                    # not again within IDENTITY_EVERY_S
    finally:
        fake.close()
    ident = mp.identity
    assert ident["Dozer9"]["ip"] == "10.200.4.50" and ident["Dozer9"]["model"] == "D71" and ident["Dozer9"]["type"] == "Bulldozer"
    assert ident["Dozer9"]["mac"] == "02-00-00-00-00-09" and "mac" not in ident["Loader3"]     # "unknown" is no MAC
    assert ident["Excavator_9"].get("zero_mac") and mp.simulated("Excavator_9") and not mp.simulated("Dozer9")
    assert simulated("SIM_TRUCK_3", {}) and simulated("X", {"ip": "127.0.0.1"})
    first = [q for q in fake.queries[:n] if 'FROM "Vai State"' in q]
    assert first and "now() - 7d" in first[0] and 'GROUP BY "Vehicle"' in first[0]
    assert not any('FROM "Id' in q or 'FROM "Vai' in q for q in fake.queries[n:])


def _fleet(now, identity, positions=None):
    vp = VehiclePositions()
    for name, (t, lat, lon) in (positions or {}).items():
        vp.position(name, lat, lon, t)
        vp.heard(name, t)
    return SimpleNamespace(positions=vp, identity=identity)


def test_which_vehicles_become_vehicles_of_their_own(tmp_path):
    now = time.time()
    mv = MobiusVehicles(tmp_path)
    ident = {"Roller7": {"ip": "10.200.4.94", "t": now}, "Truck A": {"ip": "10.200.4.11", "t": now},
             "Dozer9": {"ip": "10.200.4.50", "mac": "02-00-00-00-00-09", "model": "D71", "type": "Bulldozer", "t": now},
             "SIM_X": {"ip": "10.200.4.77", "t": now}, "Excavator_9": {"ip": "127.0.0.1", "zero_mac": True, "t": now},
             "Old": {"ip": "10.200.4.88", "t": now - KEEP_S - DAY}}
    assert mv.update(_fleet(now, ident), now)                     # new vehicles: build the inventory again
    raw = _raw()
    entries, lines = mv.entries(raw, now)
    raw2, _kept, _gone = merge_added(raw, entries)
    inv = parse_inventory(raw2)
    assert set(inv.groups) == {"roller", "truck-a", "mobius-dozer9"}                 # not SIM_X, Excavator_9, Old
    auto = inv.groups["mobius-dozer9"]
    assert auto.mobius_asset == "Dozer9" and auto.model == "D71" and inv.group_tab["mobius-dozer9"] == "vehicles"
    assert {c.type: c.host for c in auto.components} == {"vcu": None, "gnss": None, "host": "10.200.4.50"}
    assert inv.comps["mobius-dozer9-ip"].mac == "02:00:00:00:00:09"
    roller = inv.groups["roller"]                                                   # by IP: the same machine
    assert roller.mobius_asset == "Roller7" and {c.type for c in roller.components} == {"rajant", "vcu", "gnss"}
    assert inv.comps["truck-a-mobius-ip"].host == "10.200.4.11"                     # by name: its Mobius IP pinged
    assert lines == ["Added from Mobius: Dozer9 (Bulldozer · D71 · 10.200.4.50)",
                     "Roller is Roller7 in Mobius (the same IP, 10.200.4.94)"]
    assert mv.entries(raw, now)[1] == []                                           # said once
    assert not mv.update(_fleet(now, ident), now)                                  # nothing new
    ident["Dozer9"]["ip"] = "10.200.4.51"
    assert mv.update(_fleet(now, ident), now)                                      # a new IP: build again
    pub = mv.public(now)
    assert pub["groups"]["mobius-dozer9"]["auto"] and pub["groups"]["roller"]["name"] == "Roller7"
    assert {x["name"] for x in pub["others"]} >= {"SIM_X", "Excavator_9"}
    assert mv.counts() == {"known": 5, "added": 1, "linked": 2, "simulated": 2}
    again = MobiusVehicles(tmp_path)                                               # kept across a restart
    assert again.data["Dozer9"]["ip"] == "10.200.4.51" and again.told["Dozer9"] == "auto"
    raw["vehicles"].append({"id": "dozer-9", "name": "Dozer 9", "mobius_asset": "Dozer9",
                            "components": [{"id": "dozer-9-bc", "type": "rajant", "host": "10.200.4.60"}]})
    entries, lines = again.entries(raw, now)                                       # devices.xlsx has it now
    assert not any(e["group"]["id"] == "mobius-dozer9" for e in entries)
    assert lines == ["Dozer9: devices.xlsx has it now (Dozer 9) - the vehicle added from Mobius is removed"]


def test_load_site_inventory_brings_them_in(tmp_path):
    now = time.time()
    path = tmp_path / "inventory.yaml"
    path.write_text(yaml.safe_dump(_raw()), encoding="utf-8")
    mv = MobiusVehicles(tmp_path / "data")
    mv.update(_fleet(now, {"Dozer9": {"ip": "10.200.4.50", "t": now}}), now)
    inv, base, changes = load_site_inventory(path, tmp_path / "data", mobius=mv)
    assert "mobius-dozer9" in inv.groups and base["mobius-dozer9-ip"] == "10.200.4.50"
    assert changes == ["Added from Mobius: Dozer9 (10.200.4.50)"]
    inv2, _b, changes2 = load_site_inventory(path, tmp_path / "data", mobius=MobiusVehicles(tmp_path / "data"))
    assert "mobius-dozer9" in inv2.groups and changes2 == []                       # after a restart: there at once


def test_the_reader_takes_on_added_vehicles_and_swap_follows_new_addresses():
    now = time.time()
    fake = FakeInflux("mobius", _tables(now), version="3.7.0")
    try:
        inv = parse_inventory(_raw(fake.url))
        mp = MobiusPoller(inv, [inv.comps["truck-a-vcu"], inv.comps["truck-a-gnss"]], positions=VehiclePositions())
        assert mp.vehicle_of("Dozer9") is None
        mv = MobiusVehicles()
        mv.update(_fleet(now, {"Dozer9": {"ip": "10.200.4.50", "t": now}}), now)
        raw = _raw(fake.url)
        raw2, _k, _g = merge_added(raw, mv.entries(raw, now)[0])
        inv2 = parse_inventory(raw2)
        added = mp.extend(inv2, [inv2.comps["mobius-dozer9-vcu"], inv2.comps["mobius-dozer9-gnss"]])
        assert added == ["mobius-dozer9-vcu", "mobius-dozer9-gnss"] and mp.vehicle_of("Dozer9") == "mobius-dozer9"
        got = {r.comp_id: r for r in asyncio.run(mp.poll(now))}
    finally:
        fake.close()
    assert got["mobius-dozer9-vcu"].reachable and got["mobius-dozer9-gnss"].info == {"lat": 40.5, "lon": -111.9}

    # swap_inventory: new components get their pollers; a device at a new address is pinged there
    retargeted, added_pollers = [], []
    old = parse_inventory(_raw())
    new_raw = _raw()
    new_raw["vehicles"][0]["components"][0]["host"] = "10.200.4.95"
    new = parse_inventory(new_raw)
    poller = SimpleNamespace(retarget=retargeted.append)
    rt = SimpleNamespace(inv=old, base_hosts={}, history=None, mobius=None, fleet=None, can_ping=False,
                         pipeline=SimpleNamespace(swap_inventory=lambda i: None, sweep=lambda: None, unchecked={}),
                         scheduler=SimpleNamespace(poller_of=lambda cid: poller if cid == "roller-bc" else None,
                                                   add=added_pollers.extend))
    Runtime.swap_inventory(rt, new, {})
    assert retargeted == ["10.200.4.95"] and rt.inv is new


def test_the_vehicles_api():
    mv = MobiusVehicles(clock=lambda: 1000.0)
    req = lambda r: SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rt=r)))     # noqa: E731
    assert asyncio.run(mobius_vehicles(req(SimpleNamespace(mobius_vehicles=None)))) == \
        {"enabled": False, "groups": {}, "others": []}
    body = asyncio.run(mobius_vehicles(req(SimpleNamespace(mobius_vehicles=mv))))
    assert body["enabled"] and body["groups"] == {} and body["others"] == []


def test_vehicle_and_connection_rows_in_the_sheet(tmp_path):
    sheet = Sheet()
    sheet.sites.append({"Site": "lab", "Name": "Lab", "Time zone": "America/Denver", "Lat": "", "Lon": "",
                        "Mobius InfluxDB URL": "http://10.200.1.10:8086", "Mobius vehicle tag": "Vehicle",
                        "Netmaker URL": "", "Subnets": "", "Infrastructure subnet": "", "Notes": ""})
    sheet.upsert_group("lab", "Mobius Server", "server")
    sheet.upsert_device({"Site": "lab", "Group": "Mobius Server", "Type": "mobius_server", "Name": "Mobius", "IP": "10.200.1.10"})
    row = lambda typ, value, table, field="", text="": {"Site": "", "Type": typ, "Value": value, "Mobius table": table,  # noqa: E731
                                                       "Field": field, "Text values": text, "Notes": ""}
    sheet.mobius_map += [row("vehicle", "vehicle_ip", "Vai State", "Ip"), row("vehicle", "vehicle_model", "Id Model"),
                         row("mobius_server", "client_address", "Conn", "Addr"),
                         row("mobius_server", "client_state", "Conn", "State", "Connected=1, *=0")]
    path = tmp_path / "devices.xlsx"
    sheet.save(path)
    data, report = build_inventory(Sheet.load(path), "lab")
    m = data["mobius"]["map"]
    assert m["vehicle"] == {"vehicle_ip": {"measurement": "Vai State", "field": "Ip"}, "vehicle_model": {"measurement": "Id Model"}}
    assert m["mobius_server"]["client_state"] == {"measurement": "Conn", "field": "State", "values": {"Connected": 1.0, "*": 0.0}}
    assert not any("left out" in r for r in report)


def test_client_connections_by_address_never_the_user():
    now = time.time()
    u = lambda n: {"UserName": n}                                                    # noqa: E731
    conn = {"Conn": {"fields": {"Addr": "string", "State": "string"}, "tags": {"UserName": ["a person"]}, "points": [
        (now - 50, u("a person"), {"Addr": "address: ::ffff:10.200.9.5 port: 50001", "State": "Connected"}),
        (now - 900, u("a person"), {"Addr": "address: ::ffff:10.200.9.5 port: 49999", "State": "Disconnected"}),
        (now - 3000, u("someone"), {"Addr": "address: ::ffff:10.200.9.6 port: 40000", "State": "Disconnected"}),
        (now - 9000, u("someone"), {"Addr": "address: nowhere", "State": "Connected"})]}}
    fake = FakeInflux("mobius", conn)
    try:
        inv = parse_inventory({"site": SITE, "mobius": {"influx_url": fake.url, "database": "mobius", "map": {
            "mobius_server": {"client_address": {"measurement": "Conn", "field": "Addr"},
                              "client_state": {"measurement": "Conn", "field": "State", "values": {"Connected": 1, "*": 0}}}}},
            "control_room": [{"id": "mob", "name": "Mobius Server", "kind": "server", "devices": [
                {"id": "mob-svc", "type": "mobius_server", "host": "127.0.0.1"}]}]})
        r = reader_for(inv.comps["mob-svc"], inv)
        assert isinstance(r, MobiusServerReader)
        asyncio.run(r.read(now))
        asyncio.run(r.read(now))
    finally:
        fake.close()
    asked = [q for q in fake.queries if 'FROM "Conn"' in q]
    assert ["now() - 24h" in asked[0], "now() - 5m" in asked[1]] == [True, True]
    assert all("UserName" not in q and "GROUP BY" not in q for q in asked)
    pub = r.clients.public_connections(now)
    assert [(x["address"], x["connected"]) for x in pub] == [("10.200.9.5", True), ("10.200.9.6", False)]
    assert pub[0]["since"] == now - 50 and r.clients.counts(now)["connections_open"] == 1
    assert address_of("address: ::ffff:10.0.0.2 port: 59150") == "10.0.0.2" and address_of("port: 5") is None
    rt = SimpleNamespace(mobius_clients=r.clients, inv=inv)
    body = asyncio.run(mobius_clients(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rt=rt)))))
    assert body["connections"][0]["address"] == "10.200.9.5" and "a person" not in str(body)


def test_a_vehicle_ip_that_does_not_answer_never_makes_a_running_vehicle_offline():
    from networkguru.events import EventStore
    from networkguru.model import CompResult, Metric, Status
    from networkguru.pipeline import Pipeline
    from networkguru.rules import RuleSet, load_rules
    from networkguru.tsdb import MemoryTSDB
    inv = parse_inventory({"site": SITE, "vehicles": [{"id": "d9", "name": "Dozer9", "mobius_asset": "Dozer9", "components": [
        {"id": "d9-vcu", "type": "vcu"}, {"id": "d9-ip", "type": "host", "host": "10.200.4.50", "optional": True}]}]})
    p = Pipeline(inv, RuleSet(load_rules()), MemoryTSDB(), EventStore(), clock=lambda: 0)
    live = lambda t: [CompResult("d9-vcu", t, True, [Metric("vcu_comms_latency_ms", 40.0), Metric("read_ok", 1.0, {"via": "mobius"})]),  # noqa: E731
                      CompResult("d9-ip", t, False, error="no reply to ping")]
    for t in (10, 20, 30, 40):
        p.ingest(live(t), now=t)
    assert p.state["d9-ip"].status == Status.OFFLINE and p.state["d9"].status == Status.OK      # the VCU says it runs
    for t in (50, 60, 70, 80):
        p.ingest([CompResult("d9-vcu", t, False, error="not communicating with Mobius for 1 min"),
                  CompResult("d9-ip", t, False, error="no reply to ping")], now=t)
    assert p.state["d9"].status == Status.OFFLINE                                               # parked: Offline
