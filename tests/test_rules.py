import pytest

from networkguru.model import Metric, Status
from networkguru.rules import RuleMemory, RuleSet, fmt_value, load_rules, merge_rules, parse_duration, validate_rules


@pytest.fixture
def rules():
    return RuleSet(load_rules())


def ev(rules, mem, t, **metrics):
    return rules.evaluate([Metric(k, v) for k, v in metrics.items()], t, mem)


def test_thresholds_high_and_low(rules):
    mem = RuleMemory()
    assert ev(rules, mem, 0, ping_rtt_ms=20).status == Status.OK
    r = ev(rules, mem, 1, ping_rtt_ms=300)
    assert r.status == Status.WARNING and r.reasons == ["Ping RTT 300 ms ≥ 250 ms"]
    assert ev(rules, mem, 2, ping_rtt_ms=600).status == Status.CRITICAL
    soc = RuleMemory()
    assert ev(rules, soc, 0, power_soc_pct=35).status == Status.WARNING
    assert ev(rules, soc, 1, power_soc_pct=15).status == Status.CRITICAL


def test_hysteresis(rules):
    mem = RuleMemory()
    assert ev(rules, mem, 0, ping_rtt_ms=260).status == Status.WARNING
    assert ev(rules, mem, 1, ping_rtt_ms=245).status == Status.WARNING     # inside the 5 % band: stays
    assert ev(rules, mem, 2, ping_rtt_ms=230).status == Status.OK


def test_for_duration(rules):
    mem = RuleMemory()                                   # vcu_cpu_pct has for: 2m
    assert ev(rules, mem, 0, vcu_cpu_pct=97).status == Status.OK
    assert ev(rules, mem, 60, vcu_cpu_pct=97).status == Status.OK
    assert ev(rules, mem, 121, vcu_cpu_pct=97).status == Status.CRITICAL
    assert ev(rules, mem, 130, vcu_cpu_pct=20).status == Status.OK


def test_enum_and_zero(rules):
    assert ev(rules, RuleMemory(), 0, gnss_mode=4).status == Status.OK
    r = ev(rules, RuleMemory(), 0, gnss_mode=5)
    assert r.status == Status.WARNING and r.reasons == ["GNSS fix: RTK float"]
    assert ev(rules, RuleMemory(), 0, gnss_mode=2).status == Status.CRITICAL
    assert ev(rules, RuleMemory(), 0, vai_connected=0).status == Status.CRITICAL


def test_zero_offline_dominates_other_metrics(rules):
    r = ev(rules, RuleMemory(), 0, vcu_communicating=0, vcu_comms_drop_pct=100)
    assert r.status == Status.OFFLINE and r.offline


def test_component_override(rules):
    mem = RuleMemory()
    r = rules.evaluate([Metric("ping_rtt_ms", 120)], 0, mem, {"ping_rtt_ms": {"warn": 100}})
    assert r.status == Status.WARNING


def test_merge_and_validate():
    base = {"metrics": {"ping_rtt_ms": {"warn": 250, "crit": 500}}, "hysteresis": 0.05}
    out = merge_rules(base, {"metrics": {"ping_rtt_ms": {"warn": 100}}, "hysteresis": 0.1})
    assert out["metrics"]["ping_rtt_ms"] == {"warn": 100, "crit": 500} and out["hysteresis"] == 0.1
    with pytest.raises(ValueError):
        validate_rules({"metrics": {"x": {"wran": 1}}})


def test_helpers():
    assert parse_duration("2m") == 120 and parse_duration("30s") == 30 and parse_duration(5) == 5
    assert fmt_value("starlink_down_bps", 12_400_000) == "12.4 Mbps"
    assert fmt_value("power_soc_pct", 41.6) == "42%"
    assert fmt_value("power_mppt_state", 4) == "Absorption"
