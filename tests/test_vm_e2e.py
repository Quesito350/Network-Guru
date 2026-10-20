"""End-to-end against a real VictoriaMetrics (tools\\vm\\*.exe). Skipped when the exe is missing
(fetch it with:  python tools\\fetch_vm.py)."""

import asyncio
import socket
import subprocess
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient

from networkguru.__main__ import _find_vm_exe, vm_command
from networkguru.config import Settings
from networkguru.tsdb import MemoryTSDB, VictoriaTSDB
from networkguru.web.app import create_app

from .conftest import EXAMPLE

pytestmark = pytest.mark.skipif(_find_vm_exe() is None, reason="VictoriaMetrics exe not in tools\\vm")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read()


@pytest.fixture(scope="module")
def vm(tmp_path_factory):
    port = _free_port()
    store = tmp_path_factory.mktemp("vmdata")
    cmd = [a if not a.startswith("-search.latencyOffset") else "-search.latencyOffset=0s"
           for a in vm_command(_find_vm_exe(), store, port)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            _get(f"{url}/health")
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("VictoriaMetrics did not start")
    yield url
    proc.kill()
    proc.wait(10)


def flush(url):
    _get(f"{url}/internal/force_flush")


def test_same_answers_as_memory_store(vm):
    t0 = float(int(time.time()) - 7200)
    pts = [("ping_rtt_ms", {"comp": "e2e-a", "site": "e2e"}, float(v), t0 + i * 7.0)
           for i, v in enumerate([5, 9, 30, 2, 8, 40, 1, 3, 7, 11, 13, 6, 4, 2, 90, 5, 6, 7, 8, 9])]

    async def go():
        db = VictoriaTSDB(vm)
        db.write(pts)
        await db.flush()
        flush(vm)
        mem = MemoryTSDB()
        mem.write(pts)
        out = []
        for agg in ("avg", "max", "last"):
            a = await db.query_range("ping_rtt_ms", {"comp": "e2e-a"}, t0, t0 + 140, 20, agg)
            b = await mem.query_range("ping_rtt_ms", {"comp": "e2e-a"}, t0, t0 + 140, 20, agg)
            out.append((agg, a, b))
        p95 = await db.aggregate("ping_rtt_ms", {"comp": "e2e-a"}, t0 - 1, t0 + 140, "max")
        await db.close()
        return out, p95
    results, mx = asyncio.run(go())
    for agg, vmres, memres in results:
        assert len(vmres) == 1, agg
        a, b = vmres[0]["points"], memres[0]["points"]
        assert [round(t) for t, _ in a] == [round(t) for t, _ in b], agg
        assert all(abs(x[1] - y[1]) < 1e-6 for x, y in zip(a, b)), (agg, a, b)
    assert mx == 90.0


def _run_app(vm, data_dir, fresh=False):
    app = create_app(Settings(inventory=EXAMPLE, mode="sim", history_days=2, vm_url=vm, data_dir=data_dir,
                              fresh=fresh))
    with TestClient(app) as c:
        rt = app.state.rt
        for _ in range(600):
            if rt.progress.done:
                break
            time.sleep(0.1)
        assert rt.progress.done
        assert c.get("/api/meta").json()["tsdb"] == "victoriametrics"
        flush(vm)
        now = time.time()
        soc = c.get("/api/history/charts/t48-gx", params={"from": now - 86400, "to": now, "keys": "soc"}).json()
        points = len(soc[0]["series"][0]["points"])
        span = (rt.progress.start, rt.progress.end)
    flush(vm)
    return span, points


def test_history_survives_restart_and_fresh_rebuilds(vm, tmp_path):
    (s1, e1), pts1 = _run_app(vm, tmp_path)
    assert e1 - s1 > 1.9 * 86400 and pts1 > 100            # first start builds 2 days
    (s2, e2), pts2 = _run_app(vm, tmp_path)
    assert e2 - s2 < 600                                    # restart only fills the gap
    assert pts2 >= pts1 - 2                                 # history is still there
    (s3, e3), _ = _run_app(vm, tmp_path, fresh=True)
    assert e3 - s3 > 1.9 * 86400                            # --fresh throws it away and rebuilds
