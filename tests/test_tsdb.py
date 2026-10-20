import asyncio

import httpx

from networkguru.tsdb import MemoryTSDB, VictoriaTSDB, line_protocol


def run(coro):
    return asyncio.run(coro)


def test_memory_query_range_buckets():
    db = MemoryTSDB()
    db.write([("x", {"comp": "a"}, float(v), float(t)) for t, v in [(1, 1), (5, 3), (11, 10), (25, 4)]])
    res = run(db.query_range("x", {"comp": "a"}, 0, 30, 10, "avg"))
    assert res[0]["points"] == [[10, 2.0], [20, 10.0], [30, 4.0]]          # value at t = agg over (t-step, t]
    mx = run(db.query_range("x", {"comp": "a"}, 0, 30, 10, "max"))
    assert mx[0]["points"][0] == [10, 3.0]
    assert run(db.aggregate("x", {"comp": "a"}, 0, 30, "p95")) == 10.0
    assert run(db.query_range("x", {"comp": "zzz"}, 0, 30, 10)) == []


def test_memory_out_of_order_and_prune():
    db = MemoryTSDB(retention_s=100)
    db.write([("x", {}, 1.0, 50.0), ("x", {}, 2.0, 10.0), ("x", {}, 3.0, 300.0)])
    ts, _ = db.series[("x", ())]
    assert list(ts) == [10.0, 50.0, 300.0]
    db.prune(now=300)
    assert list(db.series[("x", ())][0]) == [300.0]


def test_line_protocol_escaping_and_grouping():
    lines = line_protocol([("ping_rtt_ms", {"comp": "a b", "site": "x,y"}, 1.5, 1.0),
                           ("ping_loss_pct", {"comp": "a b", "site": "x,y"}, 0.0, 1.0)])
    assert lines == [r"ng,comp=a\ b,site=x\,y ping_rtt_ms=1.5,ping_loss_pct=0.0 1000000000"]


def test_victoria_queries_and_parsing():
    seen = {}

    def handler(req: httpx.Request):
        seen.setdefault(req.url.path, []).append(req)
        if req.url.path == "/write":
            return httpx.Response(204)
        if req.url.path == "/api/v1/query_range":
            return httpx.Response(200, json={"status": "success", "data": {"result": [
                {"metric": {"__name__": "ng_ping_rtt_ms", "comp": "a"}, "values": [[100, "1.5"], [110, "2"]]}]}})
        if req.url.path == "/api/v1/query":
            return httpx.Response(200, json={"status": "success", "data": {"result": [
                {"metric": {}, "value": [200, "42"]}]}})
        return httpx.Response(404)

    async def go():
        vm = VictoriaTSDB("http://vm:8428")
        vm.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        vm.write([("ping_rtt_ms", {"comp": "a"}, 1.0, 1.0)])
        await vm.flush()
        res = await vm.query_range("ping_rtt_ms", {"comp": "a"}, 0, 200, 10, "max")
        p = await vm.aggregate("ping_rtt_ms", {"comp": "a"}, 0, 200, "p95")
        await vm.client.aclose()
        return res, p, vm.buffer
    res, p95, buf = run(go())
    assert res == [{"labels": {"comp": "a"}, "points": [[100.0, 1.5], [110.0, 2.0]]}]
    assert p95 == 42.0 and buf == []
    q = seen["/api/v1/query_range"][0].url.params["query"]
    assert q == 'max_over_time(ng_ping_rtt_ms{comp="a"}[10s])'
    assert "quantile_over_time(0.95" in seen["/api/v1/query"][0].url.params["query"]
    assert seen["/write"][0].content.startswith(b"ng,comp=a ping_rtt_ms=1.0 ")


def test_victoria_write_failure_keeps_buffer():
    async def go():
        vm = VictoriaTSDB("http://vm:8428")
        vm.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
        vm.write([("x", {}, 1.0, 1.0)])
        await vm.flush()
        await vm.client.aclose()
        return vm
    vm = run(go())
    assert len(vm.buffer) == 1 and vm.last_error
