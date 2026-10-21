"""Importers (IPAM sheet, Rajant snapshot, devices.csv), backhaul wiring, DHCP identity, change events.
Everything here is SYNTHETIC - no real site file is used, and planted fake secrets must never come out."""

import asyncio
import gzip
import zipfile

import pytest

from networkguru.events import EventStore
from networkguru.importers.build import build_inventory, classify, import_csv, import_ipam, import_rajant
from networkguru.importers.ipam import read_ipam
from networkguru.importers.protowire import field, message
from networkguru.importers.rajant_snapshot import nmea_to_deg, read_snapshot
from networkguru.importers.sheet import SITE_COLS, Sheet
from networkguru.inventory import InventoryError, normalise_mac, parse_inventory
from networkguru.model import CompResult, Metric, Status
from networkguru.pipeline import Pipeline
from networkguru.resolve import AddressBook
from networkguru.rules import RuleSet, load_rules
from networkguru.scheduler import Scheduler
from networkguru.sim.world import SimWorld
from networkguru.tsdb import MemoryTSDB

SECRET = "PLANTED-SECRET-9f3"


def site_row(site, **cols):
    row = {c: "" for c in SITE_COLS}
    row.update({"Site": site, **cols})
    return row


def _xlsx_text(path) -> str:
    with zipfile.ZipFile(path) as z:
        return " ".join(z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.endswith(".xml"))


@pytest.fixture
def ipam_file(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Tachyon User/PW", None, "Switch User/PW"])
    ws.append(["root", SECRET, "admin", SECRET])          # logins above the header - must never be read
    ws.append([])
    ws.append([])
    ws.append(["IP", "Location", "Device", "Switch Port", "Cable Color", "MM PTP SSID", "MM PTP M/S", "CH", "RSSI", "Notes"])
    rows = [
        ["10.9.0.1", "Gateway"],
        ["10.9.0.2", "COW Slipstream", "Tachyon-1", "Port 2", "Blue", "COW-T1", "M", 1, -59],
        ["10.9.0.5", "Trailer 101", "Switch MGMT", "Port 1", "White"],
        ["10.9.0.6", "Trailer 101", "Cerbo GX", "Port 2"],
        ["10.9.0.7", "Trailer 101", "Rajant", "Port 3"],
        ["10.9.0.8", "Trailer 101", "Tachion 1", "Port 4", "Purple", "T1-T2", "M", 3, -65],
        ["10.9.0.9", "Trailer 101", "Tachion 2", "Port 5", "Blue", "COW-T1", "S", 1, -60],
        ["10.9.0.10", "Trailer 102", "Switch MGMT", "Port 1"],
        ["10.9.0.12", "Trailer 102", "Rajant", "Port 3"],
        ["10.9.0.13", "Trailer 102", "Tachion 1", "Port 4", None, "T1-T2", "S", 3, -66],
        ["10.9.0.14", "Trailer 102", "Tachion 2", "Port 5", None, None, None, None, None, "Turned OFF at Switch, For Future Use"],
        ["https://example.invalid/spec"],
    ]
    for r in rows:
        ws.append(r)
    p = tmp_path / "ipam.xlsx"
    wb.save(p)
    return p


def _mendonish(ipam_file, tmp_path):
    sheet = Sheet()
    import_ipam(sheet, read_ipam(ipam_file), "lab")
    return sheet


def test_ipam_import_skips_logins_and_spares(ipam_file, tmp_path):
    rows = read_ipam(ipam_file)
    assert [r.ip for r in rows][:2] == ["10.9.0.1", "10.9.0.2"] and len(rows) == 11
    assert SECRET not in repr(rows)
    sheet = _mendonish(ipam_file, tmp_path)
    out = tmp_path / "devices.xlsx"
    sheet.save(out)
    assert SECRET not in _xlsx_text(out)
    spare = next(d for d in sheet.devices if d["IP"] == "10.9.0.14")
    assert spare["Monitor"] == "no"
    t1 = next(d for d in sheet.devices if d["IP"] == "10.9.0.9")
    assert (t1["Name"], t1["Type"], t1["Link"], t1["Role"], t1["Install RSSI"]) == ("Tachyon 2", "tachyon", "COW-T1", "slave", "-60")
    assert {g["Kind"] for g in sheet.groups} == {"core", "cow", "trailer"}


def test_redundant_backhaul_wiring_is_local(ipam_file, tmp_path):
    """Default (Mendon): Tachyon AND Rajant mesh carry management, so no trailer depends on another."""
    data, _ = build_inventory(_mendonish(ipam_file, tmp_path), "lab")
    inv = parse_inventory(data)
    c = inv.comps
    assert inv.backhaul == "redundant"
    assert c["trailer-101-switch"].depends_on is None
    for cid in ("trailer-101-rajant", "trailer-101-cerbo-gx", "trailer-101-tachyon-1", "trailer-101-tachyon-2"):
        assert c[cid].depends_on == "trailer-101-switch"
    assert c["trailer-102-tachyon-1"].depends_on == "trailer-102-switch"
    assert all(inv.comp_group[x.depends_on] == inv.comp_group[x.id] for x in c.values() if x.depends_on)
    assert c["trailer-101-tachyon-2"].uplink and inv.link_partner("trailer-101-tachyon-2") == "cow-slipstream-tachyon-1"


def test_redundant_dead_trailer_leaves_the_next_one_up(ipam_file, tmp_path):
    data, _ = build_inventory(_mendonish(ipam_file, tmp_path), "lab")
    inv = parse_inventory(data)
    ev = EventStore()
    ev.start_run(0, "t", "test")
    p = Pipeline(inv, RuleSet(load_rules()), MemoryTSDB(), ev, clock=lambda: 0)
    for t in (10, 20, 30):
        res = []
        for cid in inv.comps:
            if inv.comp_group[cid] == "trailer-101":
                res.append(CompResult(cid, t, False, error="timeout"))
            elif cid == "trailer-102-tachyon-1":                 # far end of T1-T2 is gone, radio still up
                res.append(CompResult(cid, t, True, [Metric("tachyon_link_up", 0), Metric("ping_rtt_ms", 3)]))
            else:
                res.append(CompResult(cid, t, True, [Metric("ping_rtt_ms", 2)]))
        p.ingest(res, now=t)
    s = p.state
    assert s["trailer-101-switch"].status == Status.CRITICAL
    assert s["trailer-101-rajant"].reasons == ["Unreachable via Switch"]
    assert s["trailer-101"].reasons[0] == "No device at Trailer 101 answers - power, or both backhauls down"
    assert s["trailer-102-rajant"].status == Status.OK and not s["trailer-102-rajant"].suppressed
    assert s["trailer-102-tachyon-1"].status == Status.WARNING                       # redundancy lost
    assert s["trailer-102"].reasons == ["Tachyon 1: 60 GHz link T1-T2 down"]


def test_sim_link_fault_keeps_both_radios_up(ipam_file, tmp_path):
    data, _ = build_inventory(_mendonish(ipam_file, tmp_path), "lab")
    inv = parse_inventory(data)
    t0 = 1_791_000_000.0
    w = SimWorld(inv, anchor=t0 - 40 * 86400)
    assert w.sample("trailer-101-tachyon-2", t0).info and _link(w, "trailer-101-tachyon-2", t0) == 1
    w.add_fault("trailer-101-tachyon-2", "link", 5, now=t0)
    for end in ("trailer-101-tachyon-2", "cow-slipstream-tachyon-1"):
        r = w.sample(end, t0 + 60)
        assert r.reachable and _link(w, end, t0 + 60) == 0
    assert w.sample("trailer-101-rajant", t0 + 60).reachable                      # trailer still reachable
    w.add_fault("trailer-101", "outage", 5, now=t0 + 600)                         # whole trailer dark
    assert not w.sample("trailer-101-switch", t0 + 700).reachable
    r = w.sample("trailer-102-tachyon-1", t0 + 700)
    assert r.reachable and _link(w, "trailer-102-tachyon-1", t0 + 700) == 0       # next trailer: link down only
    with pytest.raises(ValueError):
        w.add_fault("trailer-101-rajant", "link", 5)


def _link(world, cid, t):
    r = world.sample(cid, t)
    return next((m.value for m in r.metrics if m.name == "tachyon_link_up"), None)


def test_build_wires_the_backhaul_tree(ipam_file, tmp_path):
    """--backhaul tachyon: the 60 GHz links are the only path, so trailers chain."""
    data, report = build_inventory(_mendonish(ipam_file, tmp_path), "lab", backhaul="tachyon")
    inv = parse_inventory(data)
    c = inv.comps
    assert c["trailer-101-tachyon-2"].depends_on == "cow-slipstream-tachyon-1"     # uplink -> far master
    assert c["trailer-101-switch"].depends_on == "trailer-101-tachyon-2"
    assert c["trailer-101-rajant"].depends_on == "trailer-101-switch"
    assert c["trailer-102-tachyon-1"].depends_on == "trailer-101-tachyon-1"         # next trailer down the tree
    assert "trailer-102-tachyon-2" not in c                                          # spare left out
    assert c["trailer-101-tachyon-1"].rules["tachyon_rssi_dbm"] == {"low_warn": -71.0, "low_crit": -77.0}
    assert inv.link_partner("trailer-101-tachyon-2") == "cow-slipstream-tachyon-1"
    assert "1 spare" in report[0]


def test_backhaul_failure_hides_everything_behind_it(ipam_file, tmp_path):
    data, _ = build_inventory(_mendonish(ipam_file, tmp_path), "lab", backhaul="tachyon")
    inv = parse_inventory(data)
    ev = EventStore()
    ev.start_run(0, "t", "test")
    p = Pipeline(inv, RuleSet(load_rules()), MemoryTSDB(), ev, clock=lambda: 0)
    behind = {"trailer-101-tachyon-2", "trailer-101-switch", "trailer-101-cerbo-gx", "trailer-101-rajant",
              "trailer-101-tachyon-1", "trailer-102-switch", "trailer-102-rajant", "trailer-102-tachyon-1"}
    for t in (10, 20, 30):
        res = []
        for cid in inv.comps:
            if cid in behind:
                res.append(CompResult(cid, t, False, error="timeout"))
            else:
                res.append(CompResult(cid, t, True, [Metric("ping_rtt_ms", 2)]))
        p.ingest(res, now=t)
    s = p.state
    assert s["trailer-101-tachyon-2"].status == Status.CRITICAL                    # the real failure
    assert s["trailer-101-rajant"].reasons == ["Unreachable via Tachyon 2"]
    assert s["trailer-102-rajant"].reasons == ["Unreachable via Trailer 101 · Tachyon 2"]
    assert s["trailer-102"].status == Status.UNKNOWN
    assert s["trailer-102"].reasons == ["Unreachable via Trailer 101 · Tachyon 2"]


def _snapshot_bytes() -> bytes:
    def node(serial, name, ip, mode, platform="FE1", gps=None, extra=b""):
        system = message(field(2, platform), field(37, "LMB-B"), field(12, message(field(1, ip))),
                         field(19, "00:13:95:00:00:01 02:00:00:00:00:02"))
        active = message(field(1, message(field(1, name))), field(20, message(field(1, mode))),
                         field(40, message(field(1, SECRET), field(2, SECRET))))          # security block
        state = message(field(10, system), field(105, message(field(1, active), field(2, active), field(3, active))),
                        field(110, message(field(3, SECRET))))                              # imkey
        if gps:
            state += field(150, message(field(2, message(field(2, gps[0]), field(3, gps[1])))))
        return message(field(1, serial), field(2, "11.31.1"), field(4, ip), field(13, 1), field(8, state), extra)
    snap = message(field(1, "11.32.1"), field(5, f"JAVA_HOME={SECRET}"),
                   field(6, node("FE1-2255X-1", "Trailer-101", "10.9.0.7", "MANUAL", gps=("4045.60000N", "11153.40000W"))),
                   field(6, node("ME4-2450R-2", "Water Truck", "10.9.4.130", "MANUAL", platform="ME4")),
                   field(6, node("ME4-2450R-3", "Tremonton base", "10.8.252.129", "DHCP", platform="ME4")))
    return gzip.compress(snap)


def test_rajant_snapshot_reads_only_whitelisted_fields(tmp_path, ipam_file):
    mesh = tmp_path / "x.mesh"
    mesh.write_bytes(_snapshot_bytes())
    nodes = read_snapshot(mesh)
    assert [n.serial for n in nodes] == ["FE1-2255X-1", "ME4-2450R-2", "ME4-2450R-3"]
    assert nodes[0].name == "Trailer-101" and nodes[0].model == "FE1-2255X" and nodes[0].mac == "00:13:95:00:00:01"
    assert nodes[2].ip_mode == "DHCP" and abs(nodes[0].lat - 40.76) < 1e-3 and abs(nodes[0].lon + 111.89) < 1e-3
    assert SECRET not in repr(nodes)
    sheet = _mendonish(ipam_file, tmp_path)
    sheet.sites.append(site_row("tremonton", Subnets="10.8.0.0/16"))      # a Rajant there is put on that site
    import_rajant(sheet, nodes, "lab")
    t = next(d for d in sheet.devices if d["IP"] == "10.9.0.7")              # matched to the IPAM row by IP
    assert (t["Group"], t["Serial"], t["Firmware"]) == ("Trailer 101", "FE1-2255X-1", "11.31.1")
    truck = next(g for g in sheet.groups if g["Group"] == "Water Truck")
    assert truck["Kind"] == "vehicle"
    base = next(d for d in sheet.devices if d["Serial"] == "ME4-2450R-3")
    assert base["Site"] == "tremonton" and base["Address"] == "dhcp"
    out = tmp_path / "devices.xlsx"
    sheet.save(out)
    assert SECRET not in _xlsx_text(out)
    # re-import keeps Casey's edits and refreshes owned facts
    t["Name"] = "Rajant (pole)"
    nodes[0].firmware = "11.32.0"
    import_rajant(sheet, nodes, "lab")
    assert t["Name"] == "Rajant (pole)" and t["Firmware"] == "11.32.0"


def test_devices_csv_and_mobius_vehicle(tmp_path):
    sheet = Sheet()
    import_csv(sheet, [{"name": "Trailer-46", "site": "Durability", "type": "rajant", "ip": "10.9.3.7",
                        "serial": "FE1-2255X-100002", "model": "FE1", "firmware": "11.31.1"},
                       {"name": "Truck 9", "type": "rajant", "ip": "10.9.4.9", "group": "Truck 9", "kind": "vehicle",
                        "mobius_asset": "T9"}], "lab")
    data, _ = build_inventory(sheet, "lab")
    inv = parse_inventory(data)
    assert inv.comps["trailer-46-trailer-46"].serial == "FE1-2255X-100002"
    assert {c.type for c in inv.groups["truck-9"].components} == {"rajant", "vcu", "gnss"}


def test_snapshot_gps_places_locations_on_the_map(tmp_path, ipam_file):
    """The Rajant snapshot's GPS fills a location's Lat / Lon (never a vehicle's, never over a typed
    one); build-inventory carries it and the browser gets it."""
    mesh = tmp_path / "x.mesh"
    mesh.write_bytes(_snapshot_bytes())
    nodes = read_snapshot(mesh)
    sheet = _mendonish(ipam_file, tmp_path)
    import_rajant(sheet, nodes, "lab")
    t101 = sheet.group("lab", "Trailer 101")
    assert (t101["Lat"], t101["Lon"]) == ("40.760000", "-111.890000")
    truck = sheet.group("lab", "Water Truck")
    assert truck["Kind"] == "vehicle" and not truck["Lat"] and not truck["Lon"]
    t101["Lat"], t101["Lon"] = "40.7701", "-111.8801"                         # typed by hand: kept
    import_rajant(sheet, nodes, "lab")
    assert (t101["Lat"], t101["Lon"]) == ("40.7701", "-111.8801")
    out = tmp_path / "devices.xlsx"
    sheet.save(out)
    again = Sheet.load(out)
    assert (again.group("lab", "Trailer 101")["Lat"], again.group("lab", "Trailer 101")["Lon"]) == ("40.7701", "-111.8801")
    again.group("lab", "Trailer 102")["Lat"], again.group("lab", "Trailer 102")["Lon"] = "north", "-111.9"
    data, report = build_inventory(again, "lab")
    entry = next(g for g in data["infrastructure"] if g["name"] == "Trailer 101")
    assert (entry["lat"], entry["lon"]) == (40.7701, -111.8801)
    assert any("Trailer 102: Lat/Lon 'north'" in r for r in report)
    tree = parse_inventory({"site": {"id": "lab", "name": "Lab"}, "infrastructure": [
        {**{k: entry[k] for k in ("id", "name", "kind", "lat", "lon")},
         "devices": [{"id": "t101-sw", "type": "switch", "host": "10.9.0.5"}]}]}).public_tree()
    g = next(g for t in tree["tabs"] for g in t["groups"] if g["name"] == "Trailer 101")
    assert (g["lat"], g["lon"]) == (40.7701, -111.8801)


def test_service_ports_are_checked_by_type():
    sheet = Sheet()
    import_csv(sheet, [{"name": "Mobius server", "type": "mobius_server", "ip": "10.9.1.5", "group": "Servers", "kind": "server"},
                       {"name": "Switch", "type": "switch", "ip": "10.9.1.6", "group": "Servers", "kind": "server"}], "lab")
    data, _ = build_inventory(sheet, "lab")
    comps = {c["type"]: c for g in data["control_room"] for c in g["components"]}
    assert comps["mobius_server"]["checks"] == ["tcp:18080"] and "checks" not in comps["switch"]


def test_simulated_vehicles_drive_around_the_site():
    import math
    import time as _time

    from networkguru.inventory import load_inventory
    from networkguru.sim.world import SimWorld

    from .conftest import EXAMPLE
    inv = load_inventory(EXAMPLE)
    w = SimWorld(inv)
    now = _time.time()
    site = (inv.site.lat, inv.site.lon)
    for gid in w.vehicles:
        a, b = w.veh_pos(gid, now), w.veh_pos(gid, now + 60)
        assert math.dist(a, site) < 0.03 and a != b                 # within ~3 km, and moving
    gnss = next(c for c in inv.comps.values() if c.type == "gnss")
    r = w.sample(gnss.id, now)
    if r.reachable:
        assert set(r.info) == {"lat", "lon"}                          # info: kept while the vehicle is quiet


def test_sites_sheet_feeds_the_inventory(tmp_path):
    """Site name, time zone, GPS and InfluxDB come from the Sites sheet - none of it is in the code."""
    sheet = Sheet()
    import_csv(sheet, [{"name": "Trailer-1", "type": "rajant", "ip": "10.9.3.7"}], "lab")
    data, report = build_inventory(sheet, "lab")
    assert any("no Sites row for 'lab'" in r for r in report)
    assert (data["site"]["lat"], data["site"]["lon"], data["site"]["timezone"]) == (40.76, -111.89, "America/Denver")
    assert "mobius" not in data
    sheet.sites.append(site_row("lab", Name="Lab site", **{"Time zone": "America/Boise", "Lat": "43.6", "Lon": "-116.2",
                                                          "Mobius InfluxDB URL": "http://10.9.1.30:8086"}))
    out = tmp_path / "devices.xlsx"
    sheet.save(out)
    again = Sheet.load(out)
    assert again.sites == sheet.sites and again.groups == sheet.groups      # every import keeps the Sites sheet
    data, report = build_inventory(again, "lab")
    assert data["site"] == {"id": "lab", "name": "Lab site", "timezone": "America/Boise", "lat": 43.6, "lon": -116.2}
    assert data["mobius"]["influx_url"] == "http://10.9.1.30:8086" and not any("Sites row" in r for r in report)
    again.sites[0]["Lat"] = "north"
    data, report = build_inventory(again, "lab")
    assert data["site"]["lat"] == 40.76 and any("Lat 'north' is not a number" in r for r in report)


def test_subnets_on_the_sites_sheet_place_and_classify_radios():
    sheet = Sheet(sites=[site_row("lab", **{"Infrastructure subnet": "10.9.2.0/24"}),
                         site_row("other", Subnets="10.8.0.0/16, bad-entry")])
    assert sheet.site_of("10.8.1.5") == "other" and sheet.site_of("10.9.2.5") is None and sheet.site_of("") is None
    assert classify("Pole 7", "10.9.2.5", "10.9.2.0/24") == "tower"
    assert classify("Pole 7", "10.9.5.5", "10.9.2.0/24") == "vehicle" == classify("Pole 7", "10.9.2.5", "")
    assert classify("Trailer 3", "10.9.5.5", "10.9.2.0/24") == "trailer"            # the name still wins


def test_identity_rules():
    assert normalise_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff" == normalise_mac("aabb.ccdd.eeff")
    base = {"site": {"id": "x", "name": "X"}, "infrastructure": [{"id": "g", "name": "G", "devices": []}]}

    def inv_with(*devs):
        b = dict(base)
        b["infrastructure"] = [{"id": "g", "name": "G", "devices": list(devs)}]
        return parse_inventory(b)
    with pytest.raises(InventoryError, match="DHCP device needs"):
        inv_with({"id": "a", "type": "rajant", "address": "dhcp"})
    with pytest.raises(InventoryError, match="serial S1 is also on a"):
        inv_with({"id": "a", "type": "rajant", "host": "1.1.1.1", "serial": "S1"},
                 {"id": "b", "type": "rajant", "host": "1.1.1.2", "serial": "S1"})
    with pytest.raises(InventoryError, match="not 12 hex"):
        inv_with({"id": "a", "type": "rajant", "host": "1.1.1.1", "mac": "zz"})
    with pytest.raises(InventoryError, match="circle"):
        inv_with({"id": "a", "type": "rajant", "host": "1.1.1.1", "depends_on": "b"},
                 {"id": "b", "type": "rajant", "host": "1.1.1.2", "depends_on": "a"})
    inv = inv_with({"id": "a", "type": "rajant", "address": "dhcp", "mac": "AA:BB:CC:00:00:01", "serial": "S9"},
                   {"id": "b", "type": "switch"})
    assert any("no IP address yet" in w for w in inv.warnings)
    book = AddressBook(inv)
    assert book.find(serial="S9") == "a" == book.find(mac="aa-bb-cc-00-00-01")
    assert book.set("a", "10.0.0.5", "unifi", 1.0) is None and book.set("a", "10.0.0.9", "unifi", 2.0) == "10.0.0.5"


def test_dhcp_address_change_becomes_an_event(example_inv, mini_raw):
    mini_raw["infrastructure"][1]["devices"][0].update({"address": "dhcp", "serial": "FE1-X-1"})
    inv = parse_inventory(mini_raw)
    w = SimWorld(inv, anchor=1_790_000_000.0)
    t0 = 1_791_000_000.0
    a0 = w.sample("t1-bc", t0).info["address"]
    w.add_fault("t1-bc", "outage", 5, now=t0)
    a1 = w.sample("t1-bc", t0 + 3600).info["address"]
    assert a0 != a1 and a1.startswith("10.0.1.")                                   # new lease after the outage
    ev = EventStore()
    p = Pipeline(inv, RuleSet(load_rules()), MemoryTSDB(), ev, clock=lambda: t0)
    p.ingest([CompResult("t1-bc", t0, True, [Metric("ping_rtt_ms", 3)], {"address": a0})], now=t0)
    p.ingest([CompResult("t1-bc", t0 + 60, True, [Metric("ping_rtt_ms", 3)], {"address": a1})], now=t0 + 60)
    rows = ev.info_events(t0 - 1, t0 + 100)
    assert rows and rows[0]["key"] == "address" and rows[0]["reason"] == f"Address {a0} → {a1}"


def test_scheduler_rechecks_the_device_in_front(mini_inv):
    polls = {"tr1-radio": 0}

    class P:
        def __init__(self, cid, interval, ok):
            self.comp_ids, self.interval, self.timeout, self.ok = [cid], interval, 2, ok

        async def poll(self, now):
            polls[self.comp_ids[0]] = polls.get(self.comp_ids[0], 0) + 1
            return [CompResult(self.comp_ids[0], now, self.ok)]
    ev = EventStore()
    pipe = Pipeline(mini_inv, RuleSet(load_rules()), MemoryTSDB(), ev)
    sch = Scheduler(pipe, [P("tr1-radio", 60, True), P("tr1-router", 0.1, False)], tick=0.05, recheck_after=0.3)

    async def go():
        task = asyncio.create_task(sch.run())
        await asyncio.sleep(1.2)
        task.cancel()
    asyncio.run(go())
    assert polls["tr1-radio"] >= 3          # interval 60 s, but re-checked because the router failed
