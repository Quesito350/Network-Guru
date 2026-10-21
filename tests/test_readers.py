"""0.7.0 device readers against fake devices: SNMP (v2c + v3), Modbus TCP (Cerbo GX), the Mobius InfluxDB."""

import asyncio
import time

import pytest

from networkguru.drivers import NOT_MAPPED, live_pollers
from networkguru.drivers import snmp as S
from networkguru.drivers import snmp_profiles as P
from networkguru.drivers.device import DevicePoller
from networkguru.drivers.influx import InfluxClient, probe
from networkguru.drivers.mobius import MobiusPoller
from networkguru.drivers.modbus import ModbusClient, ModbusError
from networkguru.drivers.readers import ReadError
from networkguru.drivers.victron import read as read_cerbo
from networkguru.inventory import load_inventory, parse_inventory
from networkguru.model import CompResult, Metric

from .conftest import EXAMPLE
from .fake_devices import FakeInflux, FakeModbus, FakeSnmpAgent


def test_snmp_codec_and_the_rfc_key_vector():
    key = S.localize_key(S.password_to_key("maplesyrup"), bytes.fromhex("000000000000000000000002"))
    assert key.hex() == "6695febc9288e36282235fc7151f128497b38f3f"          # RFC 3414 A.3.2 (SHA)
    raw = S.pdu(S.GETBULK, 77, ["1.3.6.1.2.1.1.3.0", "1.3.6.1.4.1.34861.300.2"], 0, 20)
    assert S.parse_pdu(raw) == (S.GETBULK, 77, 0, 20, [("1.3.6.1.2.1.1.3.0", S.NULL, None),
                                                      ("1.3.6.1.4.1.34861.300.2", S.NULL, None)])
    for v in (0, 1, 127, 128, 255, 256, -1, -129, 2 ** 31 - 1):
        e = S.element(S.enc_int(v), 0)
        assert S.dec_value(S.INTEGER, S.enc_int(v)[e.a:e.b]) == v
    big = S.enc_uint(2 ** 64 - 1, S.COUNTER64)
    e = S.element(big, 0)
    assert S.dec_value(S.COUNTER64, big[e.a:e.b]) == 2 ** 64 - 1
    with pytest.raises(S.SnmpError, match="at least 8 characters"):
        S.SnmpClient("127.0.0.1", version="3", user="u", auth_password="short", priv_password="long-enough")


def _switch_values():
    v = {P.SYS_DESCR: (S.OCTETS, b"TrailerSwitch 2.1"), P.SYS_UPTIME: (S.TIMETICKS, 123456), P.SYS_NAME: (S.OCTETS, b"t1-sw")}
    for i, (typ, oper) in enumerate([(6, 1), (6, 1), (6, 2), (24, 1)], start=1):
        v[f"{P.IF_TYPE}.{i}"] = (S.INTEGER, typ)
        v[f"{P.IF_OPER}.{i}"] = (S.INTEGER, oper)
    v[f"{P.PETH_MAIN_POWER}.1"] = (S.GAUGE32, 37)
    return v


def test_snmp_v2c_switch_ports_and_poe():
    async def flow():
        agent = await FakeSnmpAgent(_switch_values(), community="ro-secret").start()
        try:
            async with S.SnmpClient("127.0.0.1", agent.port, community="ro-secret", timeout=0.5) as c:
                metrics, info = await P.read(c, "switch", {}, 0.0)
            async with S.SnmpClient("127.0.0.1", agent.port, community="wrong", timeout=0.2, retries=0) as c:
                with pytest.raises(S.SnmpError, match="no SNMP answer"):
                    await P.read(c, "switch", {}, 0.0)
        finally:
            agent.close()
        return {m.key: m.value for m in metrics}, info
    m, info = asyncio.run(flow())
    assert m == {"uptime_s": 1234.56, "switch_ports_up": 2.0, "switch_poe_w": 37.0}     # 2 Ethernet ports up
    assert info == {"hostname": "t1-sw", "description": "TrailerSwitch 2.1"}


def _rajant_values(rx, tx):
    v = {P.SYS_DESCR: (S.OCTETS, b"BreadCrumb"), P.SYS_UPTIME: (S.TIMETICKS, 500), P.SYS_NAME: (S.OCTETS, b"t1-bc")}
    for i, typ in ((1, 24), (2, 6)):
        v[f"{P.IF_TYPE}.{i}"] = (S.INTEGER, typ)
        v[f"{P.IF_OPER}.{i}"] = (S.INTEGER, 1)
        v[f"{P.IF_HC_IN}.{i}"] = (S.COUNTER64, rx if i == 2 else 10 ** 9)
        v[f"{P.IF_HC_OUT}.{i}"] = (S.COUNTER64, tx if i == 2 else 10 ** 9)
    return v


def test_snmp_v3_rajant_traffic_rates_and_login_errors():
    users = {"ngread": ("auth-pass-123", "priv-pass-456")}

    async def flow():
        agent = await FakeSnmpAgent(_rajant_values(1000, 2000), users=users).start()
        try:
            state = {}
            async with S.SnmpClient("127.0.0.1", agent.port, version="3", user="ngread", auth_password="auth-pass-123",
                                    priv_password="priv-pass-456", timeout=0.5) as c:
                first, _ = await P.read(c, "rajant", state, 100.0)
                agent.values.update(_rajant_values(1000 + 125_000, 2000 + 250_000))   # 10 s later
                second, _ = await P.read(c, "rajant", state, 110.0)
            errors = []
            for user, auth, priv in (("ngread", "wrong-auth-pass", "priv-pass-456"), ("nobody", "auth-pass-123", "priv-pass-456"),
                                     ("ngread", "auth-pass-123", "wrong-priv-pass")):
                async with S.SnmpClient("127.0.0.1", agent.port, version="3", user=user, auth_password=auth,
                                        priv_password=priv, timeout=0.5, retries=0) as c:
                    try:
                        await c.get([P.SYS_UPTIME])
                    except S.SnmpError as e:
                        errors.append(str(e))
        finally:
            agent.close()
        return first, second, errors
    first, second, errors = asyncio.run(flow())
    assert {m.key for m in first} == {"uptime_s"}                                 # no rate after one read
    m = {x.key: x.value for x in second}
    assert m["if_rx_bps"] == 100_000.0 and m["if_tx_bps"] == 200_000.0          # loopback left out
    assert errors[0] == "wrong SNMP v3 auth password" and errors[1] == "unknown SNMP v3 user"
    assert "privacy password" in errors[2]


def test_modbus_cerbo_values():
    regs = {(100, 817): 100, (100, 818): 0, (100, 819): 0, (100, 840): 268, (100, 841): 65536 - 52, (100, 842): 65536 - 140,
            (100, 843): 76, (100, 850): 320, (100, 860): 85, (239, 775): 3, (239, 784): 23}

    async def flow():
        srv = await FakeModbus(regs).start()
        try:
            async with ModbusClient("127.0.0.1", srv.port) as m:
                with_mppt = await read_cerbo(m, 239)
                plain = await read_cerbo(m, None)
                with pytest.raises(ModbusError, match="illegal data address"):
                    await m.read_holding(100, 999, 1)
        finally:
            await srv.close()
        return with_mppt, plain
    (metrics, info), (plain, _) = asyncio.run(flow())
    m = {x.key: x.value for x in metrics}
    assert m == {"power_batt_v": 26.8, "power_batt_a": -5.2, "power_batt_w": -140.0, "power_soc_pct": 76.0,
                 "power_pv_w": 320.0, "power_load_w": 185.0, "power_mppt_state": 3.0, "power_yield_today_kwh": 2.3}
    assert "power_mppt_state" not in {x.key for x in plain} and info == {}


GNSS = {"vehicle_gnss": {"fields": {"fix": "integer", "sats": "integer", "lat": "float", "lon": "float"},
                         "tags": {"asset": ["Autocar 2845", "Autocar 2846"], "site": ["lab"]}, "points": []}}


def _mobius_inv(url):
    return parse_inventory({
        "site": {"id": "lab", "name": "Lab", "timezone": "America/Denver"},
        "mobius": {"influx_url": url, "database": "mobius", "asset_tag": "asset",
                   "map": {"gnss": {"measurement": "vehicle_gnss",
                                    "fields": {"gnss_mode": "fix", "gnss_sats": "sats", "lat": "lat", "lon": "lon"}}}},
        "vehicles": [{"id": "ac2845", "name": "Autocar 2845", "mobius_asset": "Autocar 2845",
                      "devices": [{"id": "ac2845-gnss", "type": "gnss"}, {"id": "ac2845-vcu", "type": "vcu"}]},
                     {"id": "ac2846", "name": "Autocar 2846", "mobius_asset": "Autocar 2846",
                      "devices": [{"id": "ac2846-gnss", "type": "gnss"}]}]})


def test_probe_influx_lists_names_but_no_values():
    now = time.time()
    meas = {k: {**v, "points": [(now, {"asset": "Autocar 2845"}, {"fix": 4, "sats": 19, "lat": 40.7612, "lon": -111.8901})]}
            for k, v in GNSS.items()}
    influx = FakeInflux("mobius", meas)
    try:
        async def go():
            async with InfluxClient(influx.url) as c:
                return await probe(c)
        text = asyncio.run(go())
    finally:
        influx.close()
    assert "database mobius" in text and "measurement vehicle_gnss" in text and "fix (integer)" in text
    assert "asset values: Autocar 2845, Autocar 2846" in text and "_internal" not in text
    assert "40.76" not in text and "19" not in text.split("newest point")[1].split(")")[1]      # no data values


def test_mobius_reader_maps_values_positions_and_staleness():
    now = time.time()
    meas = {k: {**v, "points": [(now - 5, {"asset": "Autocar 2845"}, {"fix": 4, "sats": 19, "lat": 40.7612, "lon": -111.8901}),
                                (now - 600, {"asset": "Autocar 2846"}, {"fix": 5, "sats": 12, "lat": 40.7, "lon": -111.9})]}
            for k, v in GNSS.items()}
    influx = FakeInflux("mobius", meas)
    try:
        inv = _mobius_inv(influx.url)
        pollers, unchecked, notes = live_pollers(inv, can_ping=False)
        mp = next(p for p in pollers if isinstance(p, MobiusPoller))
        results = {r.comp_id: r for r in asyncio.run(mp.poll(now))}
    finally:
        influx.close()
    ok = results["ac2845-gnss"]
    assert ok.reachable and {m.key: m.value for m in ok.metrics} == {"gnss_mode": 4.0, "gnss_sats": 19.0, "read_ok{via=mobius}": 1.0}
    assert ok.info == {"lat": 40.7612, "lon": -111.8901}                          # feeds the map
    assert not results["ac2846-gnss"].reachable and "no Mobius data" in results["ac2846-gnss"].error
    assert unchecked["ac2845-vcu"] == NOT_MAPPED and "Mobius: 2 components read" in notes[0]


class FakeReader:
    via, label = "snmp", "SNMP v2c"

    def __init__(self, result=None, missing=()):
        self.result, self._missing, self.reads = result, list(missing), 0

    def missing(self):
        return self._missing

    async def read(self, now):
        self.reads += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_device_poller_merges_reads_caches_and_reports():
    comp = parse_inventory({"site": {"id": "lab", "name": "Lab"},
                            "infrastructure": [{"id": "g", "name": "G", "devices": [
                                {"id": "sw", "type": "switch", "host": "10.9.1.6"}]}]}).comps["sw"]
    base = CompResult("sw", 0.0, True, [Metric("ping_rtt_ms", 2.0)])
    ok = FakeReader(([Metric("switch_ports_up", 5.0)], {"hostname": "t1-sw"}))
    p = DevicePoller(comp, True, ok)
    r = p.merge(base, ok.result)
    m = {x.key: x.value for x in r.metrics}
    assert m == {"ping_rtt_ms": 2.0, "switch_ports_up": 5.0, "read_ok{via=snmp}": 1.0} and r.info["hostname"] == "t1-sw"
    r = p.merge(CompResult("sw", 10.0, False, error="no reply to ping"), None)      # between reads: the last values
    assert r.reachable and "switch_ports_up" in {x.name for x in r.metrics}          # ...and the reader proves it is up
    bad = DevicePoller(comp, True, FakeReader(ReadError("no SNMP answer (timeout)")))
    r = bad.merge(base, ReadError("no SNMP answer (timeout)"))
    assert {x.key: x.value for x in r.metrics}["read_ok{via=snmp}"] == 0.0 and r.info["read_error"].startswith("no SNMP answer")
    off = DevicePoller(comp, True, FakeReader(missing=["SWITCH_SNMP_RO"]))
    r = off.merge(base, None)
    assert "read_ok" not in {x.name for x in r.metrics} and "SWITCH_SNMP_RO" in r.info["reader"]


def test_live_pollers_name_what_is_read(monkeypatch):
    inv = load_inventory(EXAMPLE)
    for n in ("RAJANT_SNMP_USER", "RAJANT_SNMP_AUTH", "RAJANT_SNMP_PRIV", "TACHYON_SNMP_RO"):
        monkeypatch.delenv(n, raising=False)
    _p, unchecked, notes = live_pollers(inv, True)
    assert "Rajant BreadCrumb SNMP v3 (RAJANT_SNMP_AUTH, RAJANT_SNMP_PRIV, RAJANT_SNMP_USER)" in notes[1]
    assert unchecked["ac2845-vcu"] == NOT_MAPPED
    monkeypatch.setenv("TACHYON_SNMP_RO", "planted-community")
    _p, _u, notes = live_pollers(inv, True)
    assert "Tachyon 60 GHz SNMP v2c ×2" in notes[0] and "TACHYON" not in notes[1]
