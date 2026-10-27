"""0.9.0 discovery scan: ranges and their limits, NetBIOS, the scanner against fake devices on 127.0.0.x,
matching (known / moved / changed / new, learned identity), and the server side: following a moved device,
Add without a restart, the files kept across restarts, the admin API."""

import asyncio
import ipaddress
import json
import socket
import struct
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from networkguru.config import Settings
from networkguru.discovery import (Found, Ports, Scanner, ScanError, match, netbios_request, parse_netbios, ranges)
from networkguru.drivers import snmp as S
from networkguru.drivers.reach import icmp_available
from networkguru.found import apply_overlay, merge_added
from networkguru.importers.build import build_inventory, import_csv
from networkguru.importers.sheet import Sheet, read_devices_csv
from networkguru.inventory import parse_inventory
from networkguru.runtime import Runtime
from networkguru.site_settings import validate
from networkguru.web.app import create_app

from .conftest import EXAMPLE
from .fake_devices import FakeHttp, FakeModbus, FakeSnmpAgent

HDR = {"X-Requested-With": "networkguru"}
PW = "discovery-test-pass"


def _inv(**tabs):
    return parse_inventory({"site": {"id": "lab", "name": "Lab"}, **tabs})


# -- where to scan -------------------------------------------------------------------------
def test_ranges_are_the_24s_around_devices_plus_subnets_and_private_only():
    inv = _inv(infrastructure=[{"id": "t", "name": "T", "devices": [
        {"id": "a", "type": "rajant", "host": "10.200.3.7"}, {"id": "b", "type": "switch", "host": "10.200.3.9"},
        {"id": "c", "type": "starlink", "target": "10.200.5.1:9201"}, {"id": "d", "type": "host", "host": "8.8.8.8"}]}],
        discovery={"ranges": ["10.200.8.0/23"]})
    assert [str(n) for n in ranges(inv)] == ["10.200.3.0/24", "10.200.5.0/24", "10.200.8.0/23"]   # 8.8.8.8 left out
    with pytest.raises(ScanError, match="not a private network"):
        ranges(inv, ["8.8.8.0/24"])
    with pytest.raises(ScanError, match="more than one scan does"):
        ranges(inv, ["10.0.0.0/15"])
    with pytest.raises(ScanError, match="not a network"):
        ranges(inv, ["ten-two-hundred"])
    with pytest.raises(ScanError):
        ranges(inv, ["127.0.0.0/29"])                                  # loopback only in tests
    assert [str(n) for n in ranges(inv, ["127.0.0.0/29"], allow_loopback=True, around=False)] == ["10.200.8.0/23", "127.0.0.0/29"]


def _nb_answer(tid: int, name: str, mac: bytes) -> bytes:
    rd = bytes([2]) + name.ljust(15).encode() + b"\x00" + struct.pack(">H", 0x0400) \
        + b"WORKGROUP".ljust(15) + b"\x00" + struct.pack(">H", 0x8400) + mac + b"\0" * 40
    return (struct.pack(">HHHHHH", tid, 0x8400, 0, 1, 0, 0) + b"\x20" + b"CK" + b"A" * 30 + b"\x00"
            + struct.pack(">HHIH", 0x21, 1, 0, len(rd)) + rd)


def test_netbios_request_and_answer():
    q = netbios_request(0x1234)
    assert q[:12] == bytes.fromhex("123400000001000000000000") and q[12:46] == b"\x20CK" + b"A" * 30 + b"\x00"
    assert q[46:] == bytes.fromhex("00210001")
    assert parse_netbios(_nb_answer(1, "MOBIUS-PC1", bytes.fromhex("0050569a1b2c"))) == ("MOBIUS-PC1", "00:50:56:9a:1b:2c")
    assert parse_netbios(b"\x00" * 5) is None


# -- the scanner against fake devices ------------------------------------------------------
def _free(kind=socket.SOCK_STREAM) -> int:
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _NetBios(asyncio.DatagramProtocol):
    def __init__(self, name, mac):
        self.name, self.mac = name, mac

    def connection_made(self, t):
        self.t = t

    def datagram_received(self, data, addr):
        self.t.sendto(_nb_answer(struct.unpack(">H", data[:2])[0], self.name, self.mac), addr)


async def _ntrip_client(r, w):
    await r.read(256)
    w.write(b"SOURCETABLE 200 OK\r\nServer: Fake caster\r\nContent-Type: text/plain\r\n\r\nENDSOURCETABLE\r\n")
    await w.drain()
    w.close()


async def _just_accept(r, w):
    w.close()


def _rajant_agent_values():
    return {"1.3.6.1.2.1.1.1.0": (S.OCTETS, b"Rajant BreadCrumb ME4"), "1.3.6.1.2.1.1.2.0": (S.OID, "1.3.6.1.4.1.34861.1.2"),
            "1.3.6.1.2.1.1.5.0": (S.OCTETS, b"bc-14"), "1.3.6.1.4.1.34861.1.1": (S.OCTETS, b"FE1-2255X-100099")}


@pytest.mark.skipif(not icmp_available()[0], reason="ping is not permitted here")
def test_scanner_finds_and_types_the_fake_devices():
    P = Ports(mobius=_free(), influx=_free(), ntrip=_free(), modbus=_free(), snmp=_free(socket.SOCK_DGRAM),
              netbios=_free(socket.SOCK_DGRAM), extra=())

    async def flow():
        loop = asyncio.get_running_loop()
        servers = [await asyncio.start_server(_just_accept, "127.0.0.2", P.mobius),
                   await asyncio.start_server(_ntrip_client, "127.0.0.4", P.ntrip)]
        nb, _ = await loop.create_datagram_endpoint(lambda: _NetBios("MOBIUS-PC1", bytes.fromhex("0050569a1b2c")),
                                                    local_addr=("127.0.0.2", P.netbios))
        influx = FakeHttp({("GET", "/ping"): lambda _h, _b: (204, {"X-Influxdb-Version": "1.8.10"}, b"")},
                          host="127.0.0.2", port=P.influx)
        regs = {(100, 800 + i): struct.unpack(">H", "c0619ab1d2e3".encode()[2 * i:2 * i + 2])[0] for i in range(6)}
        mb = await FakeModbus(regs).start("127.0.0.4", P.modbus)
        rj = await FakeSnmpAgent(_rajant_agent_values(), community="lab-ro").start("127.0.0.3", P.snmp)
        other = await FakeSnmpAgent(_rajant_agent_values(), community="someone-elses").start("127.0.0.5", P.snmp)
        sc = Scanner([{"version": "2c", "community": "lab-ro", "name": "LAB_RO"}], can_ping=True, ports=P,
                     rate=500, timeout=0.6)
        try:
            found, tried = await sc.run([ipaddress.ip_network("127.0.0.0/29")])
        finally:
            for s in servers:
                s.close()
            nb.close()
            influx.close()
            await mb.close()
            rj.close()
            other.close()
        return {f.address: f for f in found}, tried, sc, rj, other
    found, tried, sc, rj, other = asyncio.run(flow())
    assert tried == 6
    pc, bc, base, odd = found["127.0.0.2"], found["127.0.0.3"], found["127.0.0.4"], found["127.0.0.5"]
    assert pc.roles == ["mobius_server", "influxdb"] and (pc.name, pc.mac) == ("MOBIUS-PC1", "00:50:56:9a:1b:2c")
    assert "InfluxDB 1.8.10 on port %d" % P.influx in pc.how
    assert bc.roles == ["rajant"] and (bc.serial, bc.name, bc.model) == ("FE1-2255X-100099", "bc-14", "Rajant BreadCrumb ME4")
    assert base.roles == ["gnss_base", "victron_cerbo"] and (base.serial, base.mac) == ("c0619ab1d2e3", "c0:61:9a:b1:d2:e3")
    assert odd.roles == [] and "SNMP agent (none of the site's logins fit)" in odd.how
    assert sc.login_sent_to == {"127.0.0.3", "127.0.0.5"}          # only agents that answered the no-login probe
    assert found["127.0.0.6"].roles == []


# -- matching ----------------------------------------------------------------------------------
MATCH_INV = dict(
    control_room=[{"id": "srv", "name": "Mobius Server", "kind": "server", "components": [
        {"id": "srv-mob", "type": "mobius_server", "host": "10.200.1.10"}]}],
    infrastructure=[{"id": "t9", "name": "Trailer 9", "devices": [
        {"id": "t9-bc", "type": "rajant", "host": "10.200.3.30", "serial": "FE1-2255X-100099"},
        {"id": "t9-60g", "type": "tachyon", "host": "10.200.3.31"}]}])


def test_match_known_moved_changed_new_and_learned_identity():
    inv = _inv(**MATCH_INV)
    found = [Found("10.200.1.10", ["mobius_server"], ["port 18080 open (Mobius)"], name="MOBIUS-PC1", mac="0050569a1b2c"),
             Found("10.200.3.44", ["rajant"], ["SNMP: Rajant"], name="bc-14", serial="FE1-2255X-100099"),
             Found("10.200.3.31", ["mikrotik"], ["SNMP: MikroTik"], name="sxt"),
             Found("10.200.3.50", ["influxdb"], ["InfluxDB 1.8.10 on port 8086"])]
    by = {(i["address"], i["type"]): i for i in match(found, inv)}
    pc = by[("10.200.1.10", "mobius_server")]
    assert pc["outcome"] == "known" and pc["comp"] == "srv-mob" and pc["learn"] == {"mac": "00:50:56:9a:1b:2c", "name": "MOBIUS-PC1"}
    bc = by[("10.200.3.44", "rajant")]
    assert (bc["outcome"], bc["comp"], bc["old"]) == ("moved", "t9-bc", "10.200.3.30")
    assert by[("10.200.3.31", "mikrotik")]["outcome"] == "changed"            # the Tachyon's address answers as a MikroTik
    assert by[("10.200.3.50", "influxdb")]["outcome"] == "new"
    # the learned NetBIOS name + MAC let a later move be followed
    moved = match([Found("10.200.1.77", ["mobius_server"], [], name="MOBIUS-PC1", mac="00-50-56-9A-1B-2C")], inv,
                  {"srv-mob": pc["learn"]})
    assert (moved[0]["outcome"], moved[0]["comp"]) == ("moved", "srv-mob")


def test_added_devices_merge_and_overlay_rules():
    raw = {"site": {"id": "lab", "name": "Lab"}, **MATCH_INV}
    entries = [{"tab": "control", "group": {"id": "srv", "name": "Mobius Server", "kind": "server"},
                "component": {"id": "srv-influx", "type": "influxdb", "name": "InfluxDB", "host": "10.200.1.11"}},
               {"tab": "infrastructure", "group": {"id": "trailer-12", "name": "Trailer 12", "kind": "trailer"},
                "component": {"id": "trailer-12-bc", "type": "rajant", "name": "BC", "host": "10.200.3.60",
                              "serial": "FE1-2255X-100099"}}]                     # the sheet has this serial already
    merged, notes = merge_added(raw, entries)
    inv = parse_inventory(merged)
    assert "srv-influx" in inv.comps and "trailer-12-bc" not in inv.comps and "trailer-12" not in inv.groups
    assert "is in the site inventory now" in notes[0]
    assert "srv-influx" not in parse_inventory(raw).comps                            # the file's dict is untouched
    keep, notes = apply_overlay(inv, {"t9-bc": {"address": "10.200.3.44", "was": "10.200.3.30"},
                                      "t9-60g": {"address": "10.200.3.31", "was": "10.200.3.20"},
                                      "srv-mob": {"address": "10.200.1.20", "was": "10.200.1.99"}})
    assert inv.comps["t9-bc"].host == "10.200.3.44" and list(keep) == ["t9-bc"]       # caught up / changed otherwise: dropped
    assert any("the inventory now says 10.200.1.10" in n for n in notes)


# -- the server side: follow, Add without a restart, kept across restarts -------------------------
LIVE_INV = {
    "site": {"id": "lab", "name": "Lab"},
    "control_room": [{"id": "srv", "name": "Mobius Server", "kind": "server", "components": [
        {"id": "srv-mob", "type": "mobius_server", "host": "127.0.0.21", "checks": ["tcp:9"]}]}],
    "infrastructure": [{"id": "t9", "name": "Trailer 9", "kind": "trailer", "devices": [
        {"id": "t9-bc", "type": "rajant", "host": "127.0.0.30", "serial": "FE1-2255X-100099"}]}],
}


def _live(tmp_path, inv=None):
    p = tmp_path / "inventory.yaml"
    p.write_text(yaml.safe_dump(inv or LIVE_INV), encoding="utf-8")
    return Settings(inventory=p, mode="live", vm_url=None, data_dir=tmp_path / "data", dev_admin_password=PW)


def test_runtime_follows_a_moved_device_adds_a_new_one_and_keeps_both(tmp_path):
    settings = _live(tmp_path)

    async def first():
        rt = Runtime(settings)
        await rt.start()
        try:
            d = rt.discovery
            now = time.time()
            counts = d.apply([Found("127.0.0.21", ["mobius_server"], ["port 18080 open (Mobius)"], name="MOBIUS-PC1",
                                    mac="0050569a1b2c"),
                              Found("127.0.0.44", ["rajant"], ["SNMP: Rajant"], name="bc-14", serial="FE1-2255X-100099"),
                              Found("127.0.0.60", ["tachyon"], ["SNMP: Tachyon"], name="tna-9", serial="TN30X-9")], now)
            poller = rt.scheduler.poller_of("t9-bc")
            followed = (rt.inv.comps["t9-bc"].host, poller.ping_host)
            key = next(k for k, v in d.state.get("items").items() if v["outcome"] == "new")
            cid = d.add(key, name="Tachyon 9", ctype="tachyon", group_name="Trailer 12", kind="trailer", who="Casey")
            with pytest.raises(ValueError, match="only a new device"):
                d.add(key, name="again", ctype="tachyon", group_id="t9")
            added = (cid, cid in rt.pipeline.state, rt.scheduler.poller_of(cid) is not None, rt.inv.group_tab["trailer-12"])
            pub = d.public()
            csv_text = d.export_csv()
        finally:
            await rt.stop()
        return counts, followed, added, pub, csv_text
    counts, followed, added, pub, csv_text = asyncio.run(first())
    assert counts == {"known": 1, "moved": 1, "new": 1}
    assert followed == ("127.0.0.44", "127.0.0.44")
    assert added == ("trailer-12-tachyon-9", True, True, "infrastructure")
    assert [m["new"] for m in pub["moves"]] == ["127.0.0.44"] and pub["items"][0]["outcome"] == "added"
    data = tmp_path / "data" / "state"
    assert json.loads((data / "discovery.json").read_text())["learned"]["srv-mob"] == {"mac": "00:50:56:9a:1b:2c",
                                                                                       "name": "MOBIUS-PC1"}
    rows = read_devices_csv(_write(tmp_path / "found.csv", csv_text))
    assert rows[0]["type"] == "tachyon" and rows[0]["ip"] == "127.0.0.60" and rows[0]["serial"] == "TN30X-9"
    assert (rows[0]["name"], rows[0]["group"], rows[0]["kind"]) == ("Tachyon 9", "Trailer 12", "trailer")   # as added
    sheet = Sheet()
    import_csv(sheet, rows, "lab")
    assert (sheet.devices[0]["IP"], sheet.devices[0]["Group"], sheet.groups[0]["Kind"]) == ("127.0.0.60", "Trailer 12", "trailer")

    async def again(s):
        rt = Runtime(s)
        return rt.inv.comps["t9-bc"].host, "trailer-12-tachyon-9" in rt.inv.comps, rt.notes
    host, has_added, _notes = asyncio.run(again(settings))
    assert host == "127.0.0.44" and has_added                                     # both kept across a restart
    caught_up = json.loads(json.dumps(LIVE_INV))
    caught_up["infrastructure"][0]["devices"][0]["host"] = "127.0.0.44"
    caught_up["infrastructure"].append({"id": "t12", "name": "T12", "devices": [
        {"id": "t12-60g", "type": "tachyon", "host": "127.0.0.60", "serial": "TN30X-9"}]})
    host, has_added, notes = asyncio.run(again(_live(tmp_path, caught_up)))
    assert host == "127.0.0.44" and not has_added and any("is in the site inventory now" in n for n in notes)
    assert json.loads((data / "addresses.json").read_text()) == {}                 # the sheet caught up


def _write(p, text):
    p.write_text(text, encoding="utf-8")
    return p


def test_admin_api(tmp_path):
    with TestClient(create_app(Settings(inventory=EXAMPLE, mode="sim", history_days=1, vm_url=None,
                                        data_dir=tmp_path / "sim", dev_admin_password=PW))) as c:
        assert c.get("/api/admin/discovery").status_code == 401
        c.post("/api/admin/login", json={"password": PW, "name": "Casey"}, headers=HDR)
        assert c.get("/api/admin/discovery").json() == {"enabled": False, "why": "The network scan runs in live mode only."}
        assert c.post("/api/admin/discovery/scan", headers=HDR).status_code == 409
    with TestClient(create_app(_live(tmp_path))) as c:
        c.post("/api/admin/login", json={"password": PW, "name": "Casey"}, headers=HDR)
        d = c.get("/api/admin/discovery").json()
        assert d["enabled"] and not d["running"] and d["time"] == "02:00" and {g["id"] for g in d["groups"]} == {"srv", "t9"}
        assert c.post("/api/admin/discovery/scan").status_code == 403                     # no X-Requested-With
        r = c.post("/api/admin/discovery/add", json={"key": "nope", "name": "x", "type": "host"}, headers=HDR)
        assert r.status_code == 400 and "not on the list" in r.json()["detail"]
        assert c.get("/api/admin/discovery/export.csv").text.startswith("name,site,type,ip")


def test_settings_and_sheet_subnets():
    assert validate({"discovery_time": ""}) == {"discovery_time": ""}
    assert validate({"discovery_time": "01:30"}) == {"discovery_time": "01:30"}
    with pytest.raises(ValueError):
        validate({"discovery_time": "25:00"})
    sheet = Sheet()
    sheet.sites.append({"Site": "lab", "Name": "Lab", "Time zone": "", "Lat": "", "Lon": "", "Mobius InfluxDB URL": "",
                        "Netmaker URL": "", "Subnets": "10.200.0.0/16; 10.200.250.0/24", "Infrastructure subnet": "", "Notes": ""})
    sheet.upsert_group("lab", "T1", "trailer")
    sheet.upsert_device({"Site": "lab", "Group": "T1", "Type": "rajant", "Name": "BC", "IP": "10.200.3.7"})
    data, _ = build_inventory(sheet, "lab")
    assert data["discovery"] == {"ranges": ["10.200.0.0/16", "10.200.250.0/24"]}
    assert parse_inventory(data).data.discovery.ranges == ["10.200.0.0/16", "10.200.250.0/24"]
