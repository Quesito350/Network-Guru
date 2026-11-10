"""0.9.8: a failed SNMP read says what to change on the device - by SNMP version and device type, naming the
secrets.env entry and never its value. A Tachyon with SNMPv2 off refuses a v2c read (noAccess)."""

import asyncio

from networkguru.drivers import snmp_profiles as P
from networkguru.drivers import snmp as S
from networkguru.drivers.readers import ReadError, SnmpReader
from networkguru.inventory import Component

from .fake_devices import FakeSnmpAgent

STD = {P.SYS_DESCR: (S.OCTETS, b"TNA-303X"), P.SYS_UPTIME: (S.TIMETICKS, 360000), P.SYS_NAME: (S.OCTETS, b"t")}


def _tachyon(port: int = 161) -> Component:
    return Component(id="t2", type="tachyon", name="Tachyon 2", host="127.0.0.1", snmp_version="2c",
                     snmp_port=port, snmp_community_ref="TACH_HINT_RO")


def test_a_tachyon_with_snmpv2_off_says_what_to_change(monkeypatch):
    monkeypatch.setenv("TACH_HINT_RO", "ro-hint-value")

    async def flow():
        agent = await FakeSnmpAgent(dict(STD), community="ro-hint-value", refuse_v2=True).start()
        try:
            r = SnmpReader(_tachyon(agent.port))
            try:
                await r.read(0.0)
            except ReadError as e:
                return str(e)
            return None
        finally:
            agent.close()

    msg = asyncio.run(flow())
    assert msg and msg.startswith("SNMP error: no access (item 1) - the device refused the read")
    assert "SNMPv2 is off on it" in msg and "TACH_HINT_RO" in msg
    assert "On the Tachyon's SNMP settings: SNMPv2 on" in msg
    assert "ro-hint-value" not in msg                                   # the entry's name, never its value


def test_the_same_agent_with_v2_on_is_read(monkeypatch):
    monkeypatch.setenv("TACH_HINT_RO", "ro-hint-value")

    async def flow():
        agent = await FakeSnmpAgent(dict(STD), community="ro-hint-value").start()
        try:
            return await SnmpReader(_tachyon(agent.port)).read(0.0)
        finally:
            agent.close()

    metrics, info = asyncio.run(flow())
    assert {m.name: m.value for m in metrics}["uptime_s"] == 3600.0 and info["description"] == "TNA-303X"


def test_no_answer_hints_by_type_and_version():
    t = SnmpReader(_tachyon()).hint("no SNMP answer (timeout)")
    assert "SNMP or SNMPv2 is off on it" in t and "TACH_HINT_RO" in t and "UDP 161" in t
    assert "a wrong community gets no answer" in t and "On the Tachyon's SNMP settings" in t

    sw = SnmpReader(Component(id="sw", type="switch", host="10.0.0.2", snmp_version="2c",
                              snmp_community_ref="SWITCH_SNMP_RO"))
    s = sw.hint("no SNMP answer (timeout)")
    assert "SWITCH_SNMP_RO" in s and "Tachyon" not in s
    assert "refused the read" in sw.hint("SNMP error: authorization error (item 1)")
    assert sw.hint("bad SNMP answer") == ""                             # nothing to suggest

    raj = SnmpReader(Component(id="r", type="rajant", host="10.0.0.3", snmp_version="3", snmp_user_ref="U",
                               snmp_auth_ref="A", snmp_priv_ref="P"))
    assert raj.hint("no SNMP answer (timeout)") == " - check the SNMP login and that UDP 161 is allowed from this server"
    assert raj.hint("unknown SNMP v3 user") == ""                       # v3 says what is wrong by itself
    assert raj.hint("SNMP error: no access (item 1)") == ""


def test_a_good_read_clears_the_old_read_error(mini_inv):
    from networkguru.events import EventStore
    from networkguru.model import CompResult, Metric
    from networkguru.pipeline import Pipeline
    from networkguru.rules import RuleSet, load_rules
    from networkguru.tsdb import MemoryTSDB

    ev = EventStore()
    ev.start_run(0, "t", "test")
    p = Pipeline(mini_inv, RuleSet(load_rules()), MemoryTSDB(), ev, clock=lambda: 0)
    bad = {"read_error": "SNMP error: no access (item 1) - the device refused the read", "vendor_error": "x",
           "address": "10.0.0.9"}
    p.ingest([CompResult("t1-60g", 10, True, [Metric("ping_rtt_ms", 2.0), Metric("read_ok", 0.0, {"via": "snmp"})],
                         bad)], now=10)
    assert p.state["t1-60g"].info["read_error"].startswith("SNMP error")
    p.ingest([CompResult("t1-60g", 20, True, [Metric("ping_rtt_ms", 2.0)], {"address": "10.0.0.9"})], now=20)
    assert "read_error" in p.state["t1-60g"].info                        # a ping alone says nothing about the read
    p.ingest([CompResult("t1-60g", 50, True, [Metric("tachyon_rssi_dbm", -55.0), Metric("read_ok", 1.0, {"via": "snmp"})],
                         {"address": "10.0.0.9", "peer": "COW end"})], now=50)
    info = p.state["t1-60g"].info
    assert "read_error" not in info and "vendor_error" not in info and info["peer"] == "COW end"
