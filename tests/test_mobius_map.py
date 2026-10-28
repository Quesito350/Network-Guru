"""0.9.2: the Mobius map - one table per value, from devices.xlsx's Mobius map sheet - and the readers that use it,
against a fake InfluxDB 3. Table and vehicle names are made up."""

import asyncio
import time

from networkguru.drivers import live_pollers
from networkguru.drivers.influx import InfluxClient, probe
from networkguru.drivers.mobius import MobiusPoller
from networkguru.drivers.mobius_explore import MobiusExplorer
from networkguru.drivers.mobius_map import as_number, mobius_map, normalise, parse_values
from networkguru.drivers.readers import InfluxAgeReader, MobiusServerReader, reader_for
from networkguru.importers.build import build_inventory
from networkguru.importers.sheet import Sheet
from networkguru.inventory import parse_inventory

from .fake_devices import FakeInflux

MAP = {
    "vcu": {"vcu_communicating": {"measurement": "Link State", "field": "Online"},
            "vcu_comms_latency_ms": {"measurement": "Link Latency"},
            "vcu_temp_c": "Board Temp"},
    "gnss": {"gnss_mode": {"measurement": "Fix Type", "values": {"Fixed": 4, "Float": 5, "(empty)": 0, "*": 1}},
             "gnss_sats": {"measurement": "Sats"},
             "lat": {"measurement": "Position", "field": "Lat"}, "lon": {"measurement": "Position", "field": "Lon"}},
    "mobius_server": {"mobius_assets": {"measurement": "Link State", "field": "Online"},
                      "mobius_clients": {"measurement": "Client Stats", "field": "Machine"}},
    "influxdb": {"influx_write_age_s": {"measurement": "Server Health", "field": "Total"}},
}


def _tables(now):
    v = lambda name: {"Vehicle": name}                                               # noqa: E731
    one = {"fields": {"Value": "float", "IsDegraded": "boolean"}, "tags": {"Vehicle": ["Truck A", "Truck B"]}}
    return {
        # Mobius writes these only when they change: older than the 2-minute window, inside the first day
        "Link State": {"fields": {"Online": "boolean"}, "tags": {"Vehicle": ["Truck A", "Truck B", "Truck C"]},
                       "points": [(now - 3600, v("Truck A"), {"Online": True}), (now - 7200, v("Truck B"), {"Online": False}),
                                  (now - 100, v("Truck C"), {"Online": True}), (now - 9000, v("Truck A"), {"Online": False})]},
        "Board Temp": {**one, "points": [(now - 5000, v("Truck A"), {"Value": 31.0})]},
        "Fix Type": {"fields": {"Value": "string"}, "tags": one["tags"], "points": [(now - 4000, v("Truck A"), {"Value": "Fixed"})]},
        # ... and these all the time
        "Link Latency": {**one, "points": [(now - 5, v("Truck A"), {"Value": 42.0}), (now - 7300, v("Truck B"), {"Value": 99.0})]},
        "Sats": {**one, "points": [(now - 10, v("Truck A"), {"Value": 18.0})]},
        "Position": {"fields": {"Lat": "float", "Lon": "float"}, "tags": one["tags"],
                     "points": [(now - 2, v("Truck A"), {"Lat": 40.5, "Lon": -111.9})]},
        "Client Stats": {"fields": {"Machine": "string", "Cpu": "float"}, "tags": {"UserName": ["someone"]},
                         "points": [(now - 30, {}, {"Machine": "PC-1"}), (now - 60, {}, {"Machine": "PC-1"}),
                                    (now - 120, {}, {"Machine": "PC-2"}), (now - 900, {}, {"Machine": "PC-3"})]},
        "Server Health": {"fields": {"Total": "integer"}, "tags": {}, "points": [(now - 30, {}, {"Total": 5})]},
    }


def _inv(url):
    return parse_inventory({
        "site": {"id": "lab", "name": "Lab", "timezone": "America/Denver"},
        "mobius": {"influx_url": url, "database": "mobius", "asset_tag": "Vehicle", "map": MAP},
        "control_room": [{"id": "mob", "name": "Mobius Server", "kind": "server", "devices": [
            {"id": "mob-svc", "type": "mobius_server", "host": "127.0.0.1"},
            {"id": "mob-db", "type": "influxdb", "host": "127.0.0.1"}]}],
        "vehicles": [{"id": "ta", "name": "Truck A", "mobius_asset": "Truck A",
                      "devices": [{"id": "ta-vcu", "type": "vcu"}, {"id": "ta-gnss", "type": "gnss"}]},
                     {"id": "tb", "name": "Truck B", "mobius_asset": "truck-b",       # matched loosely
                      "devices": [{"id": "tb-vcu", "type": "vcu"}, {"id": "tb-gnss", "type": "gnss"}]}]})


def test_map_shapes_and_text_values():
    assert normalise({"measurement": "vehicle_gnss", "fields": {"gnss_mode": "fix"}}) == \
        {"gnss_mode": {"measurement": "vehicle_gnss", "field": "fix", "values": {}}}           # the 0.7.0 shape
    assert normalise({"vcu_temp_c": "Board Temp"}) == {"vcu_temp_c": {"measurement": "Board Temp", "field": "Value", "values": {}}}
    spec = normalise(MAP["gnss"])["gnss_mode"]
    assert [as_number(spec, x) for x in ("Fixed", "Float ", "", "Odd", True, 3)] == [4.0, 5.0, 0.0, 1.0, 1.0, 3.0]
    assert as_number({"values": {}}, "text") is None and as_number({"values": {}}, None) is None
    assert parse_values("Fixed=4, Float=5; (empty)=0, *=0, junk") == {"Fixed": 4.0, "Float": 5.0, "(empty)": 0.0, "*": 0.0}


def test_reader_keeps_last_values_and_knows_who_is_not_communicating():
    now = time.time()
    fake = FakeInflux("mobius", _tables(now), version="3.7.0")
    try:
        inv = _inv(fake.url)
        pollers, unchecked, notes = live_pollers(inv, can_ping=False)
        mp = next(p for p in pollers if isinstance(p, MobiusPoller))
        first = {r.comp_id: r for r in asyncio.run(mp.poll(now))}
        fake.queries.clear()
        again = {r.comp_id: r for r in asyncio.run(mp.poll(now + 15))}
    finally:
        fake.close()
    for got in (first, again):          # the second poll asks only for 2 minutes and keeps the on-change values
        vcu = got["ta-vcu"]
        assert vcu.reachable and {m.key: m.value for m in vcu.metrics} == {
            "vcu_communicating": 1.0, "vcu_comms_latency_ms": 42.0, "vcu_temp_c": 31.0, "read_ok{via=mobius}": 1.0}
        gnss = got["ta-gnss"]
        assert {m.key: m.value for m in gnss.metrics} == {"gnss_mode": 4.0, "gnss_sats": 18.0, "read_ok{via=mobius}": 1.0}
        assert gnss.info == {"lat": 40.5, "lon": -111.9}
        for cid in ("tb-vcu", "tb-gnss"):                                    # the link says false: Offline, why
            assert not got[cid].reachable and got[cid].error == "not communicating with Mobius for 2 h"
    assert fake.queries and all("WHERE time > now() - 2m" in q for q in fake.queries)
    assert "Mobius: 4 components read" in notes[0] and not unchecked


def test_vehicle_without_link_value_goes_quiet_after_a_minute():
    now = time.time()
    tables = _tables(now)
    tables["Link State"]["points"] = []
    tables["Link Latency"]["points"] = [(now - 300, {"Vehicle": "Truck A"}, {"Value": 42.0})]
    for k in ("Sats", "Position"):
        tables[k]["points"] = []
    fake = FakeInflux("mobius", tables)
    try:
        mp = next(p for p in live_pollers(_inv(fake.url), can_ping=False)[0] if isinstance(p, MobiusPoller))
        got = {r.comp_id: r for r in asyncio.run(mp.poll(now))}
    finally:
        fake.close()
    assert got["ta-vcu"].error == "no Mobius data for 5 min"
    assert got["tb-vcu"].error.startswith("no Mobius data in the last day")                  # nothing at all


def test_server_counts_and_influx_age_never_ask_all_time():
    now = time.time()
    tables = _tables(now)
    fake = FakeInflux("mobius", tables, version="3.7.0")
    try:
        inv = _inv(fake.url)
        server = reader_for(inv.comps["mob-svc"], inv)
        age = reader_for(inv.comps["mob-db"], inv)
        assert isinstance(server, MobiusServerReader) and isinstance(age, InfluxAgeReader)
        m, _info = asyncio.run(server.read(now))
        assert {x.key: x.value for x in m} == {"mobius_assets": 2.0, "mobius_clients": 2.0}   # A and C; PC-1, PC-2
        m, _info = asyncio.run(age.read(now))
        assert 25 <= m[0].value <= 40 and m[0].key == "influx_write_age_s"
        tables["Server Health"]["points"] = [(now - 3 * 3600, {}, {"Total": 5})]            # nothing in the last hour
        age2 = InfluxAgeReader(inv.comps["mob-db"], inv)
        fake.queries.clear()
        m, _info = asyncio.run(age2.read(now))
        assert 3 * 3600 - 5 <= m[0].value <= 3 * 3600 + 20
        m, _info = asyncio.run(age2.read(now + 30))                     # the 7-day ask is not repeated
        assert sum("now() - 7d" in q for q in fake.queries) == 1
    finally:
        fake.close()
    selects = [q for q in fake.queries if q.startswith("SELECT")]
    assert selects and all("WHERE time > now() - " in q for q in selects)


def test_probe_and_explorer_on_influxdb_3_have_time_limits_and_no_user_names():
    now = time.time()
    fake = FakeInflux("mobius", _tables(now), version="3.7.0")
    try:
        async def go():
            async with InfluxClient(fake.url) as c:
                return await probe(c)
        text = asyncio.run(go())
        ex = MobiusExplorer(_inv(fake.url))
        asyncio.run(ex.once())
    finally:
        fake.close()
    assert "version 3.7.0" in text and "Vehicle values: Truck A, Truck B" in text and "someone" not in text
    assert "measurement Link State" in text and "newest point 20" in text
    limited = [q for q in fake.queries if q.startswith("SELECT") or q.startswith("SHOW TAG VALUES")]
    assert limited and all("time > now() - " in q for q in limited)
    order = [m for (_db, m) in ex.schema]
    assert set(order[:8]) == {"Link State", "Link Latency", "Board Temp", "Fix Type", "Sats", "Position",
                              "Client Stats", "Server Health"}                                 # mapped first
    assert {r["f"] for r in ex.group("ta")["rows"]} >= {"Lat", "Lon"}
    every = ex.everything()
    assert [u["value"] for u in every["unmatched"]] == ["Truck C"]          # never a user name, never "None"
    assert any(r["m"] == "Client Stats" for r in every["site"])


def test_mobius_map_sheet_to_inventory(tmp_path):
    sheet = Sheet()
    sheet.sites.append({"Site": "lab", "Name": "Lab", "Time zone": "America/Denver", "Lat": "", "Lon": "",
                        "Mobius InfluxDB URL": "http://10.200.1.10:8086", "Mobius vehicle tag": "Vehicle",
                        "Netmaker URL": "", "Subnets": "", "Infrastructure subnet": "", "Notes": ""})
    sheet.upsert_group("lab", "Truck A", "vehicle", **{"Mobius asset": "Truck A"})
    sheet.upsert_device({"Site": "lab", "Group": "Truck A", "Type": "rajant", "Name": "ME4", "IP": "10.200.4.30"})
    row = lambda site, typ, value, table, field="", text="": {"Site": site, "Type": typ, "Value": value,   # noqa: E731
                                                               "Mobius table": table, "Field": field, "Text values": text, "Notes": ""}
    sheet.mobius_map += [row("", "gnss", "gnss_mode", "Fix Type", "", "Fixed=4, Float=5, (empty)=0"),
                         row("", "vcu", "vcu_temp_c", "Board Temp"),
                         row("lab", "vcu", "vcu_temp_c", "Board Temp 2", "Celsius"),            # the site's own row wins
                         row("", "vcu", "not_a_value", "Odd"),
                         row("other", "vcu", "vcu_cpu_pct", "Cpu")]                            # another site's row
    path = tmp_path / "devices.xlsx"
    sheet.save(path)
    again = Sheet.load(path)
    assert len(again.mobius_map) == 5 and again.sites[0]["Mobius vehicle tag"] == "Vehicle"
    data, report = build_inventory(again, "lab")
    assert data["mobius"]["asset_tag"] == "Vehicle"
    assert data["mobius"]["map"] == {
        "gnss": {"gnss_mode": {"measurement": "Fix Type", "values": {"Fixed": 4.0, "Float": 5.0, "(empty)": 0.0}}},
        "vcu": {"vcu_temp_c": {"measurement": "Board Temp 2", "field": "Celsius"}}}
    assert any("Mobius map: row 'vcu / not_a_value / Odd' left out" in r for r in report)
    inv = parse_inventory(data)
    assert mobius_map(inv)["gnss"]["gnss_mode"]["values"][""] == 0.0
    assert {c.type for c in inv.comps.values()} >= {"vcu", "gnss"}          # a vehicle with a Mobius asset name
