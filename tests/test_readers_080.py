"""0.8.0 readers against fake devices: Rajant / Tachyon vendor values, cell modems (Sierra, MikroTik,
OpenWrt, ALMS), the Starlink dish, Netmaker WireGuard tunnels and the Mobius explorer."""

import asyncio
import json
import time

import pytest

from networkguru.drivers import NO_NETMAKER, cellular, live_pollers
from networkguru.drivers import snmp as S
from networkguru.drivers import snmp_profiles as P
from networkguru.drivers import starlink as SL
from networkguru.drivers.alms import AlmsReader
from networkguru.drivers.mobius_explore import MobiusExplorer
from networkguru.drivers.netmaker import NetmakerApi, NetmakerPoller, NetmakerReader
from networkguru.drivers.readers import LINK_RSSI, ReadError, SnmpReader, reader_for
from networkguru.importers import protowire as pw
from networkguru.importers.build import build_inventory
from networkguru.importers.sheet import Sheet
from networkguru.inventory import Component, parse_inventory
from networkguru.site_settings import validate

from .conftest import EXAMPLE
from .fake_devices import FakeHttp, FakeInflux, FakeSnmpAgent, json_reply


def _std(descr: bytes) -> dict:
    return {P.SYS_DESCR: (S.OCTETS, descr), P.SYS_UPTIME: (S.TIMETICKS, 360000), P.SYS_NAME: (S.OCTETS, b"dev")}


async def _read(values: dict, ctype: str, reads=((0.0, None),), community="ro-test"):
    """P.read() on a fake agent; each read can first change values (fn(values))."""
    agent = await FakeSnmpAgent(values, community=community).start()
    state: dict = {}
    out = []
    try:
        async with S.SnmpClient("127.0.0.1", agent.port, community=community, timeout=0.5) as c:
            for now, change in reads:
                if change:
                    change(agent)
                out.append(await P.read(c, ctype, state, now))
    finally:
        agent.close()
    return out, agent


def _m(metrics) -> dict:
    return {m.key: m.value for m in metrics}


# -- Rajant -------------------------------------------------------------------------------------
def _rajant(sfx: str) -> dict:
    R = P.RAJANT
    v = _std(b"BreadCrumb ME4")
    v[f"{R}.1.1{sfx}"] = (S.OCTETS, b"FE1-2255X-100001")
    v[f"{R}.1.2{sfx}"] = (S.INTEGER, 4512)                   # hundredths of a degree
    v[f"{R}.1.3{sfx}"] = (S.GAUGE32, 51200)
    v[f"{R}.2.7{sfx}"] = (S.COUNTER64, 100)
    for n, (name, freq, noise, busy) in enumerate([("wlan0", 5800, -96, 100), ("wlan1", 2437, -88, 200)], start=1):
        v[f"{R}.3.{n}.1{sfx}"] = (S.OCTETS, name.encode())
        v[f"{R}.3.{n}.2{sfx}"] = (S.INTEGER, freq)
        v[f"{R}.3.{n}.3{sfx}"] = (S.INTEGER, noise)
        v[f"{R}.3.{n}.4{sfx}"] = (S.TIMETICKS, 1000 * n)
        v[f"{R}.3.{n}.5{sfx}"] = (S.TIMETICKS, busy)
    return v


@pytest.mark.parametrize("sfx", ["", ".0"])          # Rajant answers on the bare OIDs; a ".0" agent works too
def test_rajant_vendor_values(sfx):
    R = P.RAJANT

    def later(agent):
        agent.set(f"{R}.2.7{sfx}", S.COUNTER64, 150)               # 50 drops in 10 s
        agent.set(f"{R}.3.1.4{sfx}", S.TIMETICKS, 2000)            # radio 1: 300 of 1000 busy = 30 %
        agent.set(f"{R}.3.1.5{sfx}", S.TIMETICKS, 400)
        agent.set(f"{R}.3.2.4{sfx}", S.TIMETICKS, 4000)            # radio 2: 800 of 2000 busy = 40 %
        agent.set(f"{R}.3.2.5{sfx}", S.TIMETICKS, 1000)
    (first, second), _ = asyncio.run(_read(_rajant(sfx), "rajant", ((0.0, None), (10.0, later))))
    m1 = _m(first[0])
    assert m1["rajant_temp_c"] == 45.1 and m1["rajant_noise_dbm"] == -88.0
    assert "rajant_busy_pct" not in m1 and "rajant_drop_pps" not in m1             # need two reads
    m, info = _m(second[0]), second[1]
    assert m["rajant_busy_pct"] == 40.0 and m["rajant_drop_pps"] == 5.0
    assert info["serial"] == "FE1-2255X-100001" and info["free_memory"] == "51,200"
    assert info["wlan0"] == "5800 MHz, noise -96 dBm, channel busy 30 %"
    assert info["wlan1"].startswith("2437 MHz, noise -88 dBm")


def test_a_failing_vendor_part_keeps_the_standard_values(monkeypatch):
    async def boom(client, state, now):
        raise S.SnmpError("SNMP error: no access (item 1)")
    monkeypatch.setitem(P.VENDOR, "rajant", boom)
    (first,), _ = asyncio.run(_read(_rajant(""), "rajant"))
    assert _m(first[0])["uptime_s"] == 3600.0
    assert first[1]["vendor_error"] == "vendor values not read: SNMP error: no access (item 1)"


# -- Tachyon ------------------------------------------------------------------------------------
def _idx(text: str) -> str:
    return f"{len(text)}." + ".".join(str(b) for b in text.encode())


def _tachyon(peers=(("aa:bb:cc:00:00:01", -55, 24, 9, 2310, "T46-far"), ("aa:bb:cc:00:00:02", -61, 19, 7, 1600, "T47-far"))) -> dict:
    T = P.TACHYON
    v = _std(b"Tachyon TNA-303L")
    v.update({f"{T}.3.1.0": (S.OCTETS, b"TNA-303L"), f"{T}.3.2.0": (S.OCTETS, b"TN30X-0001"), f"{T}.3.3.0": (S.INTEGER, 51),
              f"{T}.3.4.0": (S.OCTETS, b"1.15.0"), f"{T}.3.6.0": (S.GAUGE32, 23), f"{T}.3.7.0": (S.GAUGE32, 47),
              f"{T}.2.5.0": (S.INTEGER, len(peers))})
    r = _idx("wlan0")
    for col, val in ((2, (S.OCTETS, b"wlan0")), (4, (S.INTEGER, 2)), (5, (S.INTEGER, 60480)), (6, (S.INTEGER, 2160)),
                     (11, (S.INTEGER, 58))):
        v[f"{P.T_RADIOS}.{col}.{r}"] = val
    for mac, rx, snr, mcs, rate, name in peers:
        i = _idx(mac)
        for col, val in ((1, (S.OCTETS, mac.encode())), (6, (S.INTEGER, rx)), (7, (S.GAUGE32, rate)), (8, (S.INTEGER, snr)),
                         (10, (S.INTEGER, mcs)), (12, (S.INTEGER, 90061)), (13, (S.INTEGER, 412)),
                         (18, (S.OCTETS, b"TNA-303L")), (20, (S.OCTETS, name.encode())), (26, (S.OCTETS, b"1.15.0"))):
            v[f"{P.T_PEERS}.{col}.{i}"] = val
    return v


def test_tachyon_peer_radio_and_system_values():
    (first,), _ = asyncio.run(_read(_tachyon(), "tachyon"))
    m, info = _m(first[0]), first[1]
    assert m["tachyon_link_up"] == 1.0
    assert m["tachyon_rssi_dbm"] == -61.0 and m["tachyon_snr_db"] == 19.0                # the weaker of two peers
    assert m["tachyon_tx_mcs"] == 7.0 and m["tachyon_phy_rate_mbps"] == 1600.0
    assert m["tachyon_radio_temp_c"] == 58.0 and m["tachyon_cpu_pct"] == 23.0 and m["tachyon_mem_pct"] == 47.0
    assert info["peer"] == "T47-far" and info["peers"] == "2" and info["link_distance"] == "412 m"
    assert info["link_up_for"] == "1 d 1 h" and info["radio"] == "channel 2, 60480 MHz, 2160 MHz wide"
    assert (info["model"], info["serial"], info["firmware"], info["cpu_temperature"]) == ("TNA-303L", "TN30X-0001", "1.15.0", "51 °C")
    (none,), _ = asyncio.run(_read(_tachyon(peers=()), "tachyon"))
    assert _m(none[0])["tachyon_link_up"] == 0.0 and "tachyon_rssi_dbm" not in _m(none[0])


def test_tachyon_remote_signal_comes_from_the_other_end(monkeypatch):
    monkeypatch.setenv("TACH_TEST_RO", "ro-test")
    LINK_RSSI.clear()

    async def flow():
        a = await FakeSnmpAgent(_tachyon(peers=(("aa:bb:cc:00:00:01", -52, 25, 10, 2600, "B"),)), community="ro-test").start()
        b = await FakeSnmpAgent(_tachyon(peers=(("aa:bb:cc:00:00:02", -57, 22, 8, 2000, "A"),)), community="ro-test").start()
        mk = lambda cid, port: Component(id=cid, type="tachyon", host="127.0.0.1", link="L1", snmp_version="2c",  # noqa: E731
                                         snmp_port=port, snmp_community_ref="TACH_TEST_RO")
        ra, rb = SnmpReader(mk("end-a", a.port)), SnmpReader(mk("end-b", b.port))
        try:
            first = await ra.read(100.0)
            second = await rb.read(101.0)
            third = await ra.read(130.0)
        finally:
            a.close()
            b.close()
            for r in (ra, rb):
                r._client.close()
        return _m(first[0]), _m(second[0]), _m(third[0])
    first, second, third = asyncio.run(flow())
    assert "tachyon_remote_rssi_dbm" not in first                      # the other end not read yet
    assert second["tachyon_remote_rssi_dbm"] == -52.0
    assert third["tachyon_remote_rssi_dbm"] == -57.0


# -- cell modems -------------------------------------------------------------------------------
@pytest.mark.parametrize("model,kind", [
    ("Sierra RV55", "cell_sierra"), ("RV50X", "cell_sierra"), ("MP70", "cell_sierra"), ("XR80", "alms"),
    ("Sierra XR90", "alms"), ("MikroTik LtAP LTE6", "cell_mikrotik"), ("Chateau 5G", "cell_mikrotik"),
    ("OpenWrt x86 + EM7455", "cell_openwrt"), ("GL.iNet GL-X3000", "cell_openwrt"), ("Celona modem", None), ("", None)])
def test_cell_modem_model_picks_the_reader(model, kind):
    assert cellular.kind(model) == kind


def test_sierra_aleos_values_and_no_identity_numbers():
    SI = cellular.SIERRA
    v = _std(b"AirLink RV55")
    v.update({f"{SI}.1.261.0": (S.INTEGER, -71), f"{SI}.2.10210.0": (S.INTEGER, -98), f"{SI}.2.10209.0": (S.INTEGER, -11),
              f"{SI}.2.10211.0": (S.OCTETS, b"12.5"), f"{SI}.1.259.0": (S.OCTETS, b"Network Ready"),
              f"{SI}.1.264.0": (S.OCTETS, b"LTE"), f"{SI}.1.770.0": (S.OCTETS, b"Celona"),
              f"{SI}.1.266.0": (S.OCTETS, b"12.18"), f"{SI}.1.267.0": (S.INTEGER, 41), f"{SI}.9.7.0": (S.OCTETS, b"RV55"),
              f"{SI}.2.785.0": (S.OCTETS, b"310410123456789"), f"{SI}.2.771.0": (S.OCTETS, b"89014100000000000001")})
    (first,), agent = asyncio.run(_read(v, "cell_sierra"))
    m, info = _m(first[0]), first[1]
    assert m == {"uptime_s": 3600.0, "cell_rssi_dbm": -71.0, "cell_rsrp_dbm": -98.0, "cell_rsrq_db": -11.0,
                 "cell_sinr_db": 12.5, "cell_connected": 1.0, "cell_supply_v": 12.18, "cell_temp_c": 41.0}
    assert info["operator"] == "Celona" and info["modem_model"] == "RV55" and info["network_state"] == "Network Ready"
    assert not any(o.startswith((f"{SI}.2.785", f"{SI}.2.771")) for o in agent.sent)
    assert "310410123456789" not in json.dumps(info)


def test_mikrotik_lte_values_never_touch_imei_imsi_sim():
    MT = cellular.MT_LTE
    v = _std(b"RouterOS LtAP")
    for col, val in ((2, (S.INTEGER, -67)), (4, (S.INTEGER, -95)), (6, (S.INTEGER, 7)), (7, (S.INTEGER, 14)),
                     (11, (S.OCTETS, b"356789012345678")), (12, (S.OCTETS, b"310410999999999")),
                     (13, (S.OCTETS, b"8901410999999999999")), (15, (S.OCTETS, b"B66")), (16, (S.TIMETICKS, 9000000)),
                     (17, (S.OCTETS, b"registered")), (19, (S.OCTETS, b"R11e-LTE6")), (21, (S.INTEGER, 11)),
                     (25, (S.INTEGER, -105))):
        v[f"{MT}.{col}.2"] = val
    (first,), agent = asyncio.run(_read(v, "cell_mikrotik"))
    m, info = _m(first[0]), first[1]
    assert (m["cell_rssi_dbm"], m["cell_rsrp_dbm"], m["cell_sinr_db"], m["cell_rsrq_db"], m["cell_connected"]) == (-67, -95, 14, -10.5, 1)
    assert info["technology"] == "LTE" and info["band"] == "B66" and info["session_up_for"] == "25 h 0 min"
    assert not any(o.startswith((f"{MT}.11.", f"{MT}.12.", f"{MT}.13.")) for o in agent.sent)


def test_openwrt_extend_script_output():
    v = _std(b"OpenWrt 24.10")
    v[cellular.extend_oid()] = (S.OCTETS, b"rsrp=-101\nrsrq=-12\nsinr=8.5\nrssi=-77\noperator=Verizon_Wireless\ntech=lte\n"
                                          b"registration=registered\n")
    (first,), _ = asyncio.run(_read(v, "cell_openwrt"))
    m, info = _m(first[0]), first[1]
    assert (m["cell_rsrp_dbm"], m["cell_rsrq_db"], m["cell_sinr_db"], m["cell_rssi_dbm"], m["cell_connected"]) == (-101, -12, 8.5, -77, 1)
    assert info == {"hostname": "dev", "description": "OpenWrt 24.10", "operator": "Verizon Wireless", "technology": "lte",
                    "registration": "registered"}
    (bare,), _ = asyncio.run(_read(_std(b"OpenWrt"), "cell_openwrt"))
    assert "not set up" in bare[1]["cell"]


def test_alms_reads_xr_modems_and_keeps_only_signal(monkeypatch):
    monkeypatch.setenv("ALMS_T_ID", "client-1")
    monkeypatch.setenv("ALMS_T_SECRET", "client-secret-1")
    now_ms = time.time() * 1000

    def token(_h, body):
        ok = b"grant_type=client_credentials" in body and b"client_id=client-1" in body
        return (200, {}, json.dumps({"access_token": "tok-123", "expires_in": 3600}).encode()) if ok else (401, {}, b"no")

    def systems(h, _b):
        if h.headers.get("Authorization") != "Bearer tok-123":
            return 401, {}, b"no"
        return 200, {}, json.dumps({"count": 2, "items": [
            {"name": "Autocar cell", "lastCommDate": now_ms - 120000, "commStatus": "OK",
             "gateway": {"serialNumber": "XR80-DEMO-0001", "imei": "356000000000001"},
             "data": {"rssi": -69, "rsrp": -952, "rsrq": -105, "networkOperator": "Verizon", "imsi": "310410000000002",
                      "iccid": "89014100000000000002"}},
            {"name": "Old one", "lastCommDate": now_ms - 7200 * 1000, "commStatus": "WARNING",
             "gateway": {"serialNumber": "XR80-OLD"}, "data": {"rssi": -80}}]}).encode()
    fake = FakeHttp({("POST", "/api/oauth/token"): token, ("GET", "/api/v1/systems"): systems})
    mk = lambda serial: Component(id="c", type="cell_modem", model="XR80", serial=serial, alms_url=fake.url,  # noqa: E731
                                  alms_client_id_ref="ALMS_T_ID", alms_client_secret_ref="ALMS_T_SECRET")
    try:
        r = reader_for(mk("XR80-DEMO-0001"), None)
        assert isinstance(r, AlmsReader) and not r.missing()
        m, info = asyncio.run(r.read(time.time()))
        asyncio.run(r.read(time.time() + 30))                       # cached: no second fetch
        with pytest.raises(ReadError, match="last report was 2 h ago"):
            asyncio.run(AlmsReader(mk("XR80-OLD")).read(time.time() + 60))
        with pytest.raises(ReadError, match="not in this ALMS account"):
            asyncio.run(AlmsReader(mk("XR80-NOPE")).read(time.time() + 60))
    finally:
        fake.close()
    assert _m(m) == {"cell_rssi_dbm": -69.0, "cell_rsrp_dbm": -95.2, "cell_rsrq_db": -10.5, "cell_connected": 1.0}
    assert info["operator"] == "Verizon" and info["alms_report"] == "2 min ago" and info["alms_status"] == "OK"
    assert sum(1 for q in fake.requests if q["path"].startswith("/api/v1/systems")) == 1
    blob = json.dumps(info) + repr(m)
    assert not any(x in blob for x in ("356000000000001", "310410000000002", "89014100000000000002"))


# -- Starlink ----------------------------------------------------------------------------------
def _dish_status(outage: bool = False) -> bytes:
    st = pw.message(
        pw.field(1, pw.message(pw.field(2, "rev3_proto2"), pw.field(3, "2026.09.1.mr1"))),
        pw.field(2, pw.message(pw.field(1, 86400))),
        pw.float32(1003, 0.015),
        pw.field(1004, pw.message(pw.float32(1, 0.021))),
        pw.field(1005, pw.message(pw.field(3, 1), pw.field(9, 1))),
        pw.float32(1007, 2.5e6), pw.float32(1008, 4.0e5),
        *(() if outage else (pw.float32(1009, 33.5),)),
        *((pw.field(1014, pw.message(pw.field(1, 6))),) if outage else ()),
        pw.field(1016, 1000), pw.field(1018, 1))
    return pw.field(SL.DISH_STATUS, st)


def _grpc_web(msg: bytes | None, status: str = "0") -> bytes:
    trailer = f"grpc-status:{status}\r\ngrpc-message:\r\n".encode()
    return (SL.frame(msg) if msg is not None else b"") + b"\x80" + len(trailer).to_bytes(4, "big") + trailer


def test_starlink_request_bytes_are_the_get_status_call():
    assert SL.REQUEST == bytes.fromhex("0000000003e23e00")


def test_starlink_dish_status_through_the_nat():
    answers = {"body": _grpc_web(_dish_status())}
    fake = FakeHttp({("POST", SL.PATH): lambda _h, _b: (200, {"Content-Type": "application/grpc-web+proto"}, answers["body"])})
    target = f"127.0.0.1:{fake.port}"
    try:
        r = reader_for(Component(id="sl", type="starlink", target=target), None)
        m, info = asyncio.run(r.read(0.0))
        answers["body"] = _grpc_web(_dish_status(outage=True))
        m2, info2 = asyncio.run(SL.get_status(target))
        answers["body"] = b""
        with pytest.raises(ReadError, match="answered with nothing"):
            asyncio.run(r.read(0.0))
        answers["body"] = _grpc_web(None, status="12")
        with pytest.raises(SL.StarlinkError, match="gRPC status 12"):
            asyncio.run(SL.get_status(target))
    finally:
        fake.close()
    q = fake.requests[0]
    assert q["body"] == SL.REQUEST and q["headers"]["Content-Type"] == "application/grpc-web+proto"
    assert "Referer" not in q["headers"] and "Origin" not in q["headers"]
    assert _m(m) == {"starlink_state": 1.0, "starlink_pop_drop_pct": 1.5, "starlink_obstructed_pct": 2.1,
                     "starlink_down_bps": 2500000.0, "starlink_up_bps": 400000.0, "starlink_alerts": 1.0,
                     "starlink_pop_latency_ms": 33.5, "uptime_s": 86400.0}
    assert info == {"dish_alerts": "thermal throttle", "dish_notices": "heating", "dish_hardware": "rev3_proto2",
                    "dish_software": "2026.09.1.mr1", "dish_ethernet": "1000 Mbit/s", "dish_snr": "above the noise floor"}
    assert _m(m2)["starlink_state"] == 0.0 and info2["dish_outage"] == "outage (cause 6)"


# -- Netmaker ----------------------------------------------------------------------------------
def _netmaker(now: float, metrics=None):
    hosts = [{"id": "h1", "name": "tundra-hap", "endpointip": "10.200.9.7", "publickey": "PUBKEY-SECRET-XYZ",
              "version": "v0.30.0", "os": "linux"},
             {"id": "h2", "name": "T1-RB4011", "publickey": "PUBKEY-2"}, {"id": "h3", "name": "spare-laptop"}]
    nodes = [{"id": "n1", "hostid": "h1", "network": "site-vpn", "address": "10.200.250.11/24", "connected": True,
              "lastcheckin": int(now - 40), "status": "online", "isrelay": True},
             {"id": "n2", "hostid": "h2", "network": "site-vpn", "address": "10.200.250.12/24", "connected": True,
              "lastcheckin": int(now - 1500), "status": "offline"},
             {"id": "n3", "hostid": "h3", "network": "site-vpn", "connected": True, "lastcheckin": int(now - 10), "status": "online"}]
    routes = {("GET", "/api/hosts"): json_reply(hosts), ("GET", "/api/nodes"): json_reply(nodes),
              ("GET", "/api/metrics"): json_reply(metrics) if metrics else json_reply({"Code": 403}, 403)}
    return FakeHttp(routes)


def _nm_inv(url: str):
    wg = lambda gid, host: {"id": gid, "name": gid, "components": [   # noqa: E731
        {"id": f"{gid}-radio", "type": "rajant", "host": "127.0.0.1"},
        {"id": f"{gid}-wg", "type": "wireguard", "netmaker_host": host}]}
    return parse_inventory({"site": {"id": "t", "name": "T"}, "netmaker": {"url": url, "token_ref": "NM_TEST_TOKEN"},
                            "vehicles": [wg("tundra", "tundra-hap"), wg("t1", "t1-rb4011"), wg("ghost", "nope")],
                            "infrastructure": [{"id": "gw", "name": "Netmaker gateway", "kind": "core", "components": [
                                {"id": "gw-api", "type": "netmaker", "host": "127.0.0.1"}]}]})


def test_netmaker_tunnels_and_the_gateway_card(monkeypatch):
    monkeypatch.setenv("NM_TEST_TOKEN", "tok-nm-1")
    now = time.time()
    fake = _netmaker(now)
    try:
        inv = _nm_inv(fake.url)
        api = NetmakerApi(fake.url, "NM_TEST_TOKEN")
        poller = NetmakerPoller(inv, [c for c in inv.comps.values() if c.type == "wireguard"], api)
        res = {r.comp_id: r for r in asyncio.run(poller.poll(now))}
        gw = NetmakerReader(inv.comps["gw-api"], inv, api)
        gm, ginfo = asyncio.run(gw.read(now))
    finally:
        fake.close()
    ok = res["tundra-wg"]
    assert ok.reachable and 35 <= _m(ok.metrics)["wg_checkin_age_s"] <= 60 and _m(ok.metrics)["read_ok{via=netmaker}"] == 1
    assert ok.info["vpn_address"] == "10.200.250.11" and ok.info["netmaker_role"] == "relay" and ok.info["netclient"] == "v0.30.0"
    assert not res["t1-wg"].reachable and "last check-in 25 min ago" in res["t1-wg"].error        # matched ignoring case
    assert "no Netmaker host named 'nope'" in res["ghost-wg"].error
    assert _m(gm) == {"netmaker_nodes": 3.0, "netmaker_nodes_online": 2.0} and ginfo["not_in_inventory"] == "spare-laptop"
    assert all(q["headers"]["Authorization"] == "Bearer tok-nm-1" for q in fake.requests)
    assert [q["path"] for q in fake.requests] == ["/api/hosts", "/api/nodes", "/api/metrics"]      # one fetch for both
    assert "PUBKEY" not in repr(res) + repr(ginfo)


def test_netmaker_pro_metrics_add_latency_and_uptime(monkeypatch):
    monkeypatch.setenv("NM_TEST_TOKEN", "tok-nm-1")
    now = time.time()
    fake = _netmaker(now, metrics={"nodes": {"n1": {"connectivity": {"n3": {"connected": True, "latency": 23, "percentup": 99.5},
                                                                     "n2": {"connected": False, "latency": 999}}}}})
    try:
        inv = _nm_inv(fake.url)
        poller = NetmakerPoller(inv, [inv.comps["tundra-wg"]], NetmakerApi(fake.url, "NM_TEST_TOKEN"))
        (r,) = asyncio.run(poller.poll(now))
    finally:
        fake.close()
    assert _m(r.metrics)["wg_latency_ms"] == 23.0 and _m(r.metrics)["wg_up_pct"] == 99.5


def test_live_pollers_netmaker_and_alms_set_up(monkeypatch, example_inv):
    for k in ("NETMAKER_TOKEN", "ALMS_CLIENT_ID", "ALMS_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    _p, unchecked, notes = live_pollers(example_inv, True)
    assert unchecked["tundra-wg"] == "Not set up - add NETMAKER_TOKEN to secrets.env, then restart"
    assert unchecked["ac2845-cell"].startswith("Not set up - add ALMS_CLIENT_ID, ALMS_CLIENT_SECRET")
    assert "Netmaker (NETMAKER_TOKEN)" in notes[1]
    monkeypatch.setenv("NETMAKER_TOKEN", "x" * 8)
    monkeypatch.setenv("ALMS_CLIENT_ID", "id")
    monkeypatch.setenv("ALMS_CLIENT_SECRET", "secret-1")
    pollers, unchecked, notes = live_pollers(example_inv, True)
    nm = next(p for p in pollers if isinstance(p, NetmakerPoller))
    assert sorted(nm.comp_ids) == ["ac2845-wg", "t1-wg", "tundra-wg"] and "tundra-wg" not in unchecked
    cell = next(p for p in pollers if getattr(p, "comp_ids", None) == ["ac2845-cell"])
    assert isinstance(cell.reader, AlmsReader)
    assert "Netmaker: 3 WireGuard tunnels" in notes[0]
    raw = parse_inventory({"site": {"id": "t", "name": "T"}, "vehicles": [{"id": "v", "name": "V", "components": [
        {"id": "v-wg", "type": "wireguard"}]}]})
    assert live_pollers(raw, True)[1]["v-wg"] == NO_NETMAKER


# -- Mobius explorer ---------------------------------------------------------------------------
def test_mobius_explorer_sorts_everything_onto_the_groups(mini_raw):
    now = time.time()
    meas = {
        "vehicle_state": {"fields": {"speed": "float", "mode": "string", "api_token": "string", "lat": "float", "lon": "float"},
                          "tags": {"vehicle_name": ["Truck 1", "Truck 99"]},
                          "points": [(now - 5, {"vehicle_name": "Truck 1"}, {"speed": 3.5, "mode": "AUTO", "api_token": "leak-me",
                                                                             "lat": 40.77, "lon": -111.9}),
                                     (now - 3, {"vehicle_name": "Truck 99"}, {"speed": 1.0}),
                                     (now - 4000, {"vehicle_name": "Truck 1"}, {"speed": 9.0})]},
        "client_status": {"fields": {"cpu": "float"}, "tags": {"machine": ["OPS-PC-7"]},
                          "points": [(now - 8, {"machine": "OPS-PC-7"}, {"cpu": 12.0})]},
        "server": {"fields": {"uptime": "integer"}, "tags": {}, "points": [(now - 2, {}, {"uptime": 1234})]},
    }
    fake = FakeInflux("mobius", meas)
    mini_raw["control_room"].append({"id": "desk1", "name": "Ops desk", "kind": "client", "components": [
        {"id": "desk1-pc", "type": "mobius_client", "host": "127.0.0.1", "mobius_machine_id": "OPS-PC-7"}]})
    mini_raw["mobius"] = {"influx_url": fake.url, "database": "mobius"}
    try:
        ex = MobiusExplorer(parse_inventory(mini_raw))
        asyncio.run(ex.once())
    finally:
        fake.close()
    truck = {r["f"]: r["v"] for r in ex.group("truck1")["rows"]}
    assert truck == {"speed": 3.5, "mode": "AUTO", "lat": 40.77, "lon": -111.9}                # no api_token, not the old point
    assert {r["f"]: r["v"] for r in ex.group("desk1")["rows"]} == {"cpu": 12.0}
    assert ex.schema[("mobius", "vehicle_state")]["who"] == "vehicle_name"
    summary = ex.summary()
    assert summary["positions"]["truck1"]["lat"] == 40.77 and set(summary["groups"]) == {"truck1", "desk1"}
    every = ex.everything()
    assert [(u["tag"], u["value"]) for u in every["unmatched"]] == [("vehicle_name", "Truck 99")]
    assert every["site"][0]["f"] == "uptime" and every["site"][0]["v"] == 1234
    report = json.dumps(ex.report())
    assert ex.report()["not_in_inventory"] == 1 and "Truck 99" not in report and "AUTO" not in report


# -- sheet, settings ---------------------------------------------------------------------------
def test_build_inventory_netmaker_starlink_and_cell_modems():
    sheet = Sheet()
    sheet.sites.append({"Site": "lab", "Name": "Lab", "Time zone": "America/Denver", "Lat": "", "Lon": "",
                        "Mobius InfluxDB URL": "", "Netmaker URL": "https://netmaker.example.com:8443/", "Subnets": "",
                        "Infrastructure subnet": "", "Notes": ""})
    sheet.upsert_group("lab", "Truck 1", "vehicle", **{"Netmaker host": "truck1-hap"})
    sheet.upsert_group("lab", "Trailer 5", "trailer")
    for row in ({"Group": "Truck 1", "Type": "rajant", "Name": "ME4", "IP": "10.200.4.30"},
                {"Group": "Truck 1", "Type": "cell_modem", "Name": "Backup LTE", "Model": "XR80", "Serial": "N123"},
                {"Group": "Trailer 5", "Type": "cell_modem", "Name": "LTE", "Model": "RV55", "IP": "10.200.3.40"},
                {"Group": "Trailer 5", "Type": "cell_modem", "Name": "Odd", "Model": "Brick 3000", "IP": "10.200.3.41"},
                {"Group": "Trailer 5", "Type": "starlink", "Name": "Starlink", "API address": "10.200.3.1:9201"}):
        sheet.upsert_device({"Site": "lab", **row})
    data, report = build_inventory(sheet, "lab")
    inv = parse_inventory(data)
    assert data["netmaker"] == {"url": "https://netmaker.example.com:8443", "token_ref": "NETMAKER_TOKEN"}
    wg = inv.comps["truck-1-wg"]
    assert wg.type == "wireguard" and wg.model_extra["netmaker_host"] == "truck1-hap"
    gw = inv.comps["netmaker-api"]
    assert gw.type == "netmaker" and gw.host == "netmaker.example.com" and gw.checks == ["tcp:8443"]
    xr = inv.comps["truck-1-backup-lte"]
    assert xr.model_extra["alms_client_id_ref"] == "ALMS_CLIENT_ID" and "snmp_community_ref" not in xr.model_extra
    assert inv.comps["trailer-5-lte"].model_extra["snmp_community_ref"] == "CELL_SNMP_RO"
    assert inv.comps["trailer-5-starlink"].target == "10.200.3.1:9201"
    assert any("cell modem model 'Brick 3000' not recognised" in r for r in report)
    assert "NETMAKER_TOKEN" in inv.missing_secrets(env={})
    assert not any("no IP address" in w for w in inv.warnings)                     # the XR80 needs none


def test_hide_quiet_settings_are_checked():
    assert validate({"hide_quiet_min": "90", "hide_quiet_tabs": ["infrastructure", "vehicles"]}) == {
        "hide_quiet_min": 90, "hide_quiet_tabs": ["vehicles", "infrastructure"]}
    with pytest.raises(ValueError, match="hide_quiet_min"):
        validate({"hide_quiet_min": -1})
    with pytest.raises(ValueError, match="hide_quiet_tabs"):
        validate({"hide_quiet_tabs": ["map"]})


def test_example_inventory_has_the_new_types():
    from networkguru.inventory import load_inventory
    inv = load_inventory(EXAMPLE)
    types = {c.type for c in inv.comps.values()}
    assert {"wireguard", "netmaker", "starlink", "cell_modem"} <= types
