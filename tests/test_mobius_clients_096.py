"""0.9.6: every Mobius client PC Mobius reports, listed by itself - read with the Mobius Server's count from the
clients table (no tag, the PC in a field), its values from the map's mobius_client rows, matched to a desk of
the inventory where there is one. Against the fake InfluxDB; the names and tables are made up."""

import asyncio
import time
from types import SimpleNamespace

from networkguru.drivers.readers import MobiusServerReader, reader_for
from networkguru.importers.build import build_inventory
from networkguru.importers.sheet import Sheet
from networkguru.inventory import parse_inventory
from networkguru.mobius_clients import CONNECTED_S, FORGET_S, MobiusClients
from networkguru.web.api import mobius_clients

from .fake_devices import FakeInflux

MAP = {"mobius_server": {"mobius_clients": {"measurement": "Client Stats", "field": "Machine"}},
       "mobius_client": {"host_cpu_pct": {"measurement": "Client Stats", "field": "Cpu"},
                         "host_mem_used_pct": {"measurement": "Client Stats", "field": "Mem"},
                         "client_fps": {"measurement": "Other Table", "field": "Fps"}}}     # another table: not read


def _inv(url):
    return parse_inventory({
        "site": {"id": "lab", "name": "Lab", "timezone": "America/Denver"},
        "mobius": {"influx_url": url, "database": "mobius", "asset_tag": "Vehicle", "map": MAP},
        "control_room": [
            {"id": "mob", "name": "Mobius Server", "kind": "server", "devices": [
                {"id": "mob-svc", "type": "mobius_server", "host": "127.0.0.1"}]},
            {"id": "desk-2", "name": "Ops desk 2", "kind": "client", "devices": [
                {"id": "desk-2-pc", "type": "mobius_client", "name": "PC 2", "host": "127.0.0.1"}]}]})


def _table(now):
    pc = lambda t, name, cpu, mem: (t, {}, {"Machine": name, "Cpu": cpu, "Mem": mem})     # noqa: E731
    return {"Client Stats": {"fields": {"Machine": "string", "Cpu": "float", "Mem": "float"}, "tags": {},
                             "points": [pc(now - 30, "PC-1", 10.0, 40.0), pc(now - 90, "PC-1", 50.0, 41.0),
                                        pc(now - 200, "PC-2", 3.0, 25.0), pc(now - 400, "PC-3", 9.0, 9.0)]}}


def test_the_server_reader_lists_every_client():
    now = time.time()
    fake = FakeInflux("mobius", _table(now))
    try:
        inv = _inv(fake.url)
        r = reader_for(inv.comps["mob-svc"], inv)
        assert isinstance(r, MobiusServerReader)
        metrics, _info = asyncio.run(r.read(now))
    finally:
        fake.close()
    assert {m.key: m.value for m in metrics} == {"mobius_clients": 2.0}         # PC-3 wrote 400 s ago
    q = [x for x in fake.queries if 'FROM "Client Stats"' in x]
    assert q == ['SELECT "Machine", "Cpu", "Mem" FROM "Client Stats" WHERE time > now() - 5m']
    pub = {c["name"]: c for c in r.clients.public(inv, now)}
    assert set(pub) == {"PC-1", "PC-2"} and r.clients.columns == ["host_cpu_pct", "host_mem_used_pct"]
    assert pub["PC-1"]["values"] == {"host_cpu_pct": 10.0, "host_mem_used_pct": 40.0}   # its newest row
    assert pub["PC-1"]["connected"] and pub["PC-1"]["group"] is None
    assert pub["PC-2"]["group"] == "desk-2"                                      # the desk whose PC is named so
    later = {c["name"]: c for c in r.clients.public(inv, now + CONNECTED_S)}    # nothing new since: last seen
    assert not later["PC-1"]["connected"] and later["PC-1"]["last"] == now - 30
    assert r.clients.public(inv, now + FORGET_S + 60) == []                     # a day unseen: dropped
    assert r.clients.counts(now) == {"seen": 2, "connected": 2, "connections_open": 0}


def test_without_client_values_the_names_still_list():
    mc = MobiusClients(clock=lambda: 1000.0)
    mc.seen("PC-9", 990.0, {})
    mc.seen("PC-9", 900.0, {"host_cpu_pct": 99.0})                             # older: kept out
    inv = parse_inventory({"site": {"id": "lab", "name": "Lab", "timezone": "America/Denver"}})
    assert mc.public(inv) == [{"name": "PC-9", "connected": True, "last": 990.0, "values": {}, "group": None}]


def test_the_api():
    mc = MobiusClients(clock=lambda: 1000.0)
    mc.columns = ["host_cpu_pct"]
    mc.seen("PC-1", 995.0, {"host_cpu_pct": 12.0})
    inv = parse_inventory({"site": {"id": "lab", "name": "Lab", "timezone": "America/Denver"}})
    req = lambda rt: SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rt=rt)))     # noqa: E731
    body = asyncio.run(mobius_clients(req(SimpleNamespace(mobius_clients=mc, inv=inv))))
    assert body["enabled"] and body["columns"] == ["host_cpu_pct"] and body["clients"][0]["values"] == {"host_cpu_pct": 12.0}
    assert asyncio.run(mobius_clients(req(SimpleNamespace(mobius_clients=None, inv=inv)))) == \
        {"enabled": False, "clients": [], "columns": [], "connections": []}


def test_mobius_client_rows_in_the_sheet(tmp_path):
    sheet = Sheet()
    sheet.sites.append({"Site": "lab", "Name": "Lab", "Time zone": "America/Denver", "Lat": "", "Lon": "",
                        "Mobius InfluxDB URL": "http://10.200.1.10:8086", "Mobius vehicle tag": "Vehicle",
                        "Netmaker URL": "", "Subnets": "", "Infrastructure subnet": "", "Notes": ""})
    sheet.upsert_group("lab", "Mobius Server", "server")
    sheet.upsert_device({"Site": "lab", "Group": "Mobius Server", "Type": "mobius_server", "Name": "Mobius", "IP": "10.200.1.10"})
    row = lambda typ, value, field: {"Site": "", "Type": typ, "Value": value, "Mobius table": "Client Stats",   # noqa: E731
                                     "Field": field, "Text values": "", "Notes": ""}
    sheet.mobius_map += [row("mobius_server", "mobius_clients", "Machine"), row("mobius_client", "host_cpu_pct", "Cpu"),
                         row("mobius_client", "host_disk_used_pct", "Disk"), row("mobius_client", "client_uptime_s", "Up")]
    path = tmp_path / "devices.xlsx"
    sheet.save(path)
    data, report = build_inventory(Sheet.load(path), "lab")
    assert data["mobius"]["map"]["mobius_client"] == {
        "host_cpu_pct": {"measurement": "Client Stats", "field": "Cpu"},
        "host_disk_used_pct": {"measurement": "Client Stats", "field": "Disk"},
        "client_uptime_s": {"measurement": "Client Stats", "field": "Up"}}
    assert not any("left out" in r for r in report)
