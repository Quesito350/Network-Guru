from networkguru.events import EventStore
from networkguru.model import CompResult, Metric, Status
from networkguru.pipeline import Pipeline
from networkguru.rules import RuleSet, load_rules
from networkguru.tsdb import MemoryTSDB

ALL_OK = {
    "srv-mob": {"mobius_up": 1, "mobius_clients": 2},
    "srv-ntrip": {"ntrip_connected": 1},
    "core-sw": {"ping_rtt_ms": 1},
    "t1-bc": {"ping_rtt_ms": 8},
    "t1-gx": {"power_soc_pct": 80},
    "t1-60g": {"tachyon_rssi_dbm": -50},
    "tr1-radio": {"ping_rtt_ms": 20},
    "tr1-router": {"ping_rtt_ms": 21},
    "tr1-vcu": {"vcu_communicating": 1, "vcu_comms_latency_ms": 40},
    "tr1-gnss": {"gnss_mode": 4},
}


def make(inv):
    ev = EventStore()
    ev.start_run(0, "t", "test")
    return Pipeline(inv, RuleSet(load_rules()), MemoryTSDB(), ev, clock=lambda: 0), ev


def results(t, overrides=None, down=()):
    out = []
    for cid, m in ALL_OK.items():
        if cid in down:
            out.append(CompResult(cid, t, False, error="timeout"))
            continue
        mm = {**m, **(overrides or {}).get(cid, {})}
        out.append(CompResult(cid, t, True, [Metric(k, v) for k, v in mm.items()]))
    return out


def test_all_ok_rolls_up(mini_inv):
    p, _ = make(mini_inv)
    p.ingest(results(10), now=10)
    assert p.state["truck1"].status == Status.OK
    assert p.state["vehicles"].status == Status.OK
    assert p.state["infrastructure"].counts == {"ok": 2}


def test_warning_rolls_up_with_reason(mini_inv):
    p, ev = make(mini_inv)
    p.ingest(results(10, {"t1-gx": {"power_soc_pct": 30}}), now=10)
    assert p.state["t1"].status == Status.WARNING
    assert p.state["t1"].reasons == ["Cerbo GX: Battery 30% ≤ 40%"] or "Battery 30%" in p.state["t1"].reasons[0]
    assert p.state["infrastructure"].status == Status.WARNING
    assert any(r["node"] == "t1" and r["new"] == Status.WARNING for r in ev.events(0, 100))


def test_offline_needs_n_failed_polls(mini_inv):
    p, _ = make(mini_inv)
    p.ingest(results(10), now=10)
    p.ingest(results(20, down=("core-sw",)), now=20)
    assert p.state["core-sw"].status == Status.OK                 # one miss is not an outage
    p.ingest(results(30, down=("core-sw",)), now=30)
    p.ingest(results(40, down=("core-sw",)), now=40)
    assert p.state["core-sw"].status == Status.CRITICAL           # infrastructure offline = critical
    assert p.state["core-sw"].reasons[0].startswith("Offline")


def test_parked_vehicle_is_offline_not_critical_and_children_suppressed(mini_inv):
    p, _ = make(mini_inv)
    p.ingest(results(10), now=10)
    parked = {"tr1-vcu": {"vcu_communicating": 0}}
    for t in (20, 30, 40):
        p.ingest(results(t, parked, down=("tr1-radio", "tr1-router", "tr1-gnss")), now=t)
    s = p.state
    assert s["tr1-radio"].status == Status.OFFLINE
    assert s["tr1-router"].status == Status.UNKNOWN and s["tr1-router"].suppressed
    assert s["tr1-router"].reasons == ["Unreachable via Rajant BreadCrumb"]
    assert s["tr1-vcu"].status == Status.UNKNOWN and s["tr1-vcu"].suppressed
    assert s["truck1"].status == Status.OFFLINE
    assert s["vehicles"].status == Status.OFFLINE                  # every vehicle parked


def test_expected_online_makes_offline_critical(mini_raw):
    from networkguru.inventory import parse_inventory
    mini_raw["vehicles"][0]["expected_online"] = "always"
    inv = parse_inventory(mini_raw)
    p, _ = make(inv)
    for t in (10, 20, 30):
        p.ingest(results(t, down=("tr1-radio",)), now=t)
    assert p.state["tr1-radio"].status == Status.CRITICAL
    assert p.state["truck1"].status == Status.CRITICAL


def test_stale_data_goes_unknown(mini_inv):
    p, _ = make(mini_inv)
    p.ingest(results(10), now=10)
    p.sweep(now=10 + 4 * 10 + 1)                                  # VCU polls every 10 s, stale after 4
    assert p.state["tr1-vcu"].status == Status.UNKNOWN
    assert p.state["tr1-vcu"].reasons[0].startswith("No data since")


def test_metrics_and_status_written(mini_inv):
    p, _ = make(mini_inv)
    p.ingest(results(10), now=10)
    keys = {k[0] for k in p.tsdb.series}
    assert {"ping_rtt_ms", "power_soc_pct", "status"} <= keys
    labels = dict(next(k for k in p.tsdb.series if k[0] == "power_soc_pct")[1])
    assert labels == {"comp": "t1-gx", "site": "testsite", "type": "victron_cerbo"}


def test_delta_only_has_changed_nodes(mini_inv):
    p, _ = make(mini_inv)
    p.ingest(results(10), now=10)
    v = p.version
    p.ingest([r for r in results(20) if r.comp_id == "t1-gx"], now=20)
    d = p.delta_since(v)
    assert "t1-gx" in d["nodes"] and "tr1-vcu" not in d["nodes"]
