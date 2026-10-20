from networkguru.events import EventStore
from networkguru.model import Status

OK, UNK, OFF, WARN, CRIT = (int(s) for s in (Status.OK, Status.UNKNOWN, Status.OFFLINE, Status.WARNING, Status.CRITICAL))


def store_with(changes, run=(0, 1000)):
    ev = EventStore()
    ev.start_run(run[0], "t", "test")
    ev.beat(run[1])
    ev.add_changes([(ts, node, "component", None, new, "", 0) for ts, node, new in changes])
    return ev


def test_intervals_and_availability():
    ev = store_with([(0, "a", OK), (100, "a", CRIT), (200, "a", OK)])
    segs = ev.intervals(["a"], 0, 1000, now=1000)["a"]
    assert segs == [[0, 100, OK], [100, 200, CRIT], [200, 1000, OK]]
    av = ev.availability(["a"], 0, 1000, now=1000)["a"]
    assert abs(av["pct"] - 90.0) < 1e-9


def test_offline_not_counted_against_availability():
    ev = store_with([(0, "v", OK), (500, "v", OFF)])
    av = ev.availability(["v"], 0, 1000, now=1000)["v"]
    assert av["pct"] == 100.0 and av["seconds"]["offline"] == 500


def test_collector_gap_is_unknown():
    ev = EventStore()
    ev.add_changes([(0, "a", "component", None, OK, "", 0)])
    ev.start_run(0, "t", "test")
    ev.beat(300)                       # run 1 covers 0 .. 300 (+90 s slack)
    ev.start_run(700, "t", "test")     # run 2 from 700 (current)
    segs = ev.intervals(["a"], 0, 1000, now=1000)["a"]
    assert segs == [[0, 390, OK], [390, 700, UNK], [700, 1000, OK]]
    assert abs(ev.availability(["a"], 0, 1000, now=1000)["a"]["pct"] - 100.0) < 1e-9


def test_status_before_range_is_used():
    ev = store_with([(0, "a", WARN)], run=(0, 5000))
    assert ev.intervals(["a"], 1000, 2000, now=5000)["a"] == [[1000, 2000, WARN]]


def test_events_filtering():
    ev = store_with([(10, "a", WARN), (20, "a", OK), (30, "b", CRIT)])
    ev.add_changes([(40, "c", "component", OK, UNK, "Unreachable via x", 1)])
    rows = ev.events(0, 100)
    assert [r["node"] for r in rows] == ["b", "a", "a"]            # newest first, suppressed hidden
    assert len(ev.events(0, 100, include_suppressed=True)) == 4
    assert [r["node"] for r in ev.events(0, 100, min_status=CRIT)] == ["b"]
