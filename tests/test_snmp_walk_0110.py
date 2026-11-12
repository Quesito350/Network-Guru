"""0.11.0 All SNMP values: the subtrees read and named (interfaces by their name, the vendor values we know),
limits, SIM-like text hidden and secrets masked, v3, one read at a time with a short cache, and the API's
refusals (a cell modem, sim mode). Made-up devices only."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from networkguru.config import Settings, register_secret
from networkguru.drivers import snmp as S
from networkguru.drivers import snmp_walk as W
from networkguru.inventory import Component
from networkguru.web.app import create_app

from .conftest import EXAMPLE
from .fake_devices import FakeSnmpAgent

R = "1.3.6.1.4.1.34861"


def _values() -> dict:
    return {
        "1.3.6.1.2.1.1.1.0": (S.OCTETS, b"BreadCrumb ME4"),
        "1.3.6.1.2.1.1.3.0": (S.TIMETICKS, 9_000_000),
        "1.3.6.1.2.1.1.5.0": (S.OCTETS, b"bc-lab"),
        "1.3.6.1.2.1.2.1.0": (S.INTEGER, 2),
        "1.3.6.1.2.1.2.2.1.2.1": (S.OCTETS, b"eth0 long description"),
        "1.3.6.1.2.1.2.2.1.2.2": (S.OCTETS, b"wlan0"),
        "1.3.6.1.2.1.2.2.1.6.1": (S.OCTETS, bytes.fromhex("020000aabbcc")),
        "1.3.6.1.2.1.2.2.1.10.1": (S.COUNTER32, 12345),
        "1.3.6.1.2.1.2.2.1.10.2": (S.COUNTER32, 678),
        "1.3.6.1.2.1.31.1.1.1.1.1": (S.OCTETS, b"eth0"),
        "1.3.6.1.2.1.31.1.1.1.6.1": (S.COUNTER64, 10 ** 12),
        f"{R}.1.1": (S.OCTETS, b"FE1-LAB-0001"),
        f"{R}.1.2": (S.INTEGER, 4512),
        f"{R}.3.1.1": (S.OCTETS, b"wlan0"),
        f"{R}.9.1": (S.OCTETS, b"89014103211118510720"),             # SIM-like
        f"{R}.9.2": (S.OCTETS, b"login lab-walk-secret-9 here"),
        f"{R}.9.3": (S.INTEGER, 77),                                 # unknown to us: its number
        "1.3.6.1.6.3.15.1.2.2.1.2.1": (S.OCTETS, b"never-read-user"),  # the v3 user table: not a subtree read
    }


def _walk(values, ctype="rajant", **kw):
    async def go():
        agent = await FakeSnmpAgent(values, community="ro-walk").start()
        try:
            async with S.SnmpClient("127.0.0.1", agent.port, community="ro-walk", timeout=0.5) as c:
                return await W.walk_all(c, ctype, **kw)
        finally:
            agent.close()
    return asyncio.run(go())


def test_everything_named_and_hidden_and_masked():
    register_secret("lab-walk-secret-9")
    got = _walk(_values())
    rows = {r["oid"]: r for r in got["rows"]}
    assert not got["cut"] and not got["skipped"]
    assert rows["1.3.6.1.2.1.1.1.0"]["name"] == "sysDescr" and rows["1.3.6.1.2.1.1.1.0"]["s"] == "System"
    assert rows["1.3.6.1.2.1.1.3.0"]["v"].startswith("9000000 (1 d ") and rows["1.3.6.1.2.1.1.3.0"]["k"] == "time"
    assert rows["1.3.6.1.2.1.2.2.1.10.1"]["name"] == "ifInOctets · eth0"           # ifName wins over ifDescr
    assert rows["1.3.6.1.2.1.2.2.1.10.2"]["name"] == "ifInOctets · wlan0" and rows["1.3.6.1.2.1.2.2.1.10.2"]["v"] == 678
    assert rows["1.3.6.1.2.1.31.1.1.1.6.1"]["name"] == "ifHCInOctets · eth0"
    assert rows["1.3.6.1.2.1.2.2.1.6.1"]["v"] == "02:00:00:aa:bb:cc"                # a MAC
    assert rows[f"{R}.1.1"]["name"] == "serial number" and rows[f"{R}.1.1"]["s"] == "Vendor"
    assert rows[f"{R}.3.1.1"]["name"] == "radio 1 name"
    assert rows[f"{R}.9.3"]["name"] == "" and rows[f"{R}.9.3"]["v"] == 77
    assert rows[f"{R}.9.1"]["v"] == W.HIDDEN and rows[f"{R}.9.1"]["k"] == "hidden"
    assert "lab-walk-secret-9" not in rows[f"{R}.9.2"]["v"]
    assert not any(o.startswith("1.3.6.1.6.3") for o in rows)                      # the v3 user table is never read


def test_limits_and_a_switch_has_no_vendor_tree():
    got = _walk(_values(), max_values=4)
    assert got["cut"] and len(got["rows"]) == 4
    sw = _walk(_values(), ctype="switch")
    assert not any(r["s"] == "Vendor" for r in sw["rows"])


def test_a_refused_system_tree_is_an_error_and_v3_works():
    async def refused():
        agent = await FakeSnmpAgent(_values(), community="ro-walk", refuse_v2=True).start()
        try:
            async with S.SnmpClient("127.0.0.1", agent.port, community="ro-walk", timeout=0.5) as c:
                await W.walk_all(c, "tachyon")
        finally:
            agent.close()
    with pytest.raises(S.SnmpError, match="no access"):
        asyncio.run(refused())

    async def v3():
        agent = await FakeSnmpAgent(_values(), users={"ngwalk": ("auth-pass-123", "priv-pass-456")}).start()
        try:
            async with S.SnmpClient("127.0.0.1", agent.port, version="3", user="ngwalk", auth_password="auth-pass-123",
                                    priv_password="priv-pass-456", timeout=0.5) as c:
                return await W.walk_all(c, "rajant")
        finally:
            agent.close()
    assert any(r["name"] == "serial number" for r in asyncio.run(v3())["rows"])


def test_one_read_at_a_time_cached_and_per_address(monkeypatch):
    monkeypatch.setenv("WALK_TEST_RO", "ro-walk")

    async def go():
        agent = await FakeSnmpAgent(_values(), community="ro-walk").start()
        try:
            mk = lambda cid: Component(id=cid, type="rajant", host="127.0.0.1", snmp_version="2c",  # noqa: E731
                                       snmp_port=agent.port, snmp_community_ref="WALK_TEST_RO")
            walks = W.SnmpWalks()
            first = walks.start(mk("bc-a"), "192.0.2.9")
            assert first["state"] == "running"
            with pytest.raises(ValueError, match="Another device"):
                walks.start(mk("bc-b"), "192.0.2.8")
            await walks.task
            done = walks.get("bc-a")
            assert done["state"] == "done" and any(r["name"] == "sysName" for r in done["rows"])
            assert walks.start(mk("bc-a"), "192.0.2.9")["state"] == "done" and walks.running is None   # cached
            with pytest.raises(ValueError, match="moment ago"):
                walks.start(mk("bc-b"), "192.0.2.9")                                  # the same address, too soon
            unset = Component(id="bc-c", type="rajant", host="127.0.0.1", snmp_version="2c", snmp_community_ref="NOT_SET_X")
            with pytest.raises(ValueError, match="not set up"):
                walks.start(unset, "192.0.2.7")
        finally:
            agent.close()
    asyncio.run(go())


def test_api_refusals(tmp_path):
    app = create_app(Settings(inventory=EXAMPLE, mode="sim", history_days=1, vm_url=None, data_dir=tmp_path))
    hdr = {"X-Requested-With": "networkguru"}
    with TestClient(app) as c:
        comps = c.app.state.rt.inv.comps
        rajant = next(k for k, v in comps.items() if v.type == "rajant" and v.host)
        modem = next((k for k, v in comps.items() if v.type == "cell_modem"), None)
        assert c.get(f"/api/snmp/{rajant}/all").json()["state"] == "none"
        assert c.post(f"/api/snmp/{rajant}/all").status_code == 403                    # same origin only
        assert c.post(f"/api/snmp/{rajant}/all", headers=hdr).status_code == 409       # sim mode: no device to read
        if modem:
            assert c.post(f"/api/snmp/{modem}/all", headers=hdr).status_code == 400    # SIM numbers: never
        assert c.post("/api/snmp/nope/all", headers=hdr).status_code == 404
