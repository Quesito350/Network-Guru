import asyncio

from networkguru.events import EventStore
from networkguru.pipeline import Pipeline
from networkguru.rules import RuleSet, load_rules
from networkguru.sim.backfill import backfill
from networkguru.sim.noise import episode, rnd, vnoise
from networkguru.sim.world import SimWorld, lfp_voltage, sun_height
from networkguru.tsdb import MemoryTSDB

T0 = 1_791_000_000.0          # fixed time so the tests never depend on the clock


def test_noise_is_deterministic_and_bounded():
    assert rnd(1, 2, 3) == rnd(1, 2, 3)
    vals = [vnoise(7, T0 + i * 37, 600) for i in range(2000)]
    assert min(vals) >= -1 and max(vals) <= 1
    assert episode(5, T0, 0.0, 10, 100) is None


def test_sun_and_battery_curve():
    noon_utc = 1_792_000_800.0 - (1_792_000_800 % 86400) + 19.5 * 3600     # ~13:30 MDT
    assert sun_height(40.76, -111.89, noon_utc) > 0.5
    assert sun_height(40.76, -111.89, noon_utc + 12 * 3600) == 0.0
    assert lfp_voltage(100) > lfp_voltage(50) > lfp_voltage(10)


def test_world_is_deterministic(example_inv):
    a = SimWorld(example_inv, anchor=T0 - 40 * 86400)
    b = SimWorld(example_inv, anchor=T0 - 40 * 86400)
    for cid in ("t48-gx", "ac2846-vcu", "core-starlink", "mobius-svc", "bldg-north-60g"):
        ra, rb = a.sample(cid, T0), b.sample(cid, T0)
        assert ra.reachable == rb.reachable and [m.value for m in ra.metrics] == [m.value for m in rb.metrics]


def test_every_type_produces_catalog_metrics(example_inv):
    from networkguru.catalog import METRICS
    w = SimWorld(example_inv, anchor=T0 - 40 * 86400)
    for cid in example_inv.comps:
        for t in (T0, T0 + 6 * 3600):
            r = w.sample(cid, t)
            for m in r.metrics:
                assert m.name in METRICS, f"{cid}: {m.name} not in catalog"


def test_fault_injection(example_inv):
    w = SimWorld(example_inv, anchor=T0 - 40 * 86400)
    assert w.sample("t46-bc", T0).reachable
    w.add_fault("trailer-46", "outage", 5, now=T0)
    assert not w.sample("t46-bc", T0 + 60).reachable
    assert w.sample("t46-bc", T0 + 600).reachable


def test_backfill_one_day(example_inv):
    ev = EventStore()
    db = MemoryTSDB()
    p = Pipeline(example_inv, RuleSet(load_rules()), db, ev, clock=lambda: T0)
    w = SimWorld(example_inv, anchor=T0 - 40 * 86400)
    ev.start_run(T0 - 86400, "t", "sim")
    n = asyncio.run(backfill(w, p, ev, db, T0 - 86400, T0))
    assert n > 200
    assert all(p.state[g].status is not None for g in example_inv.groups)
    assert db.points_written > 10_000
    # the worn trailer battery dips below the 40 % warning at some point in a day
    soc = asyncio.run(db.query_range("power_soc_pct", {"comp": "t48-gx"}, T0 - 86400, T0, 3600, "min"))
    assert soc and min(v for _, v in soc[0]["points"]) < 60
