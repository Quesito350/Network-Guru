"""Time-series storage for the numeric metrics.

VictoriaTSDB  - VictoriaMetrics single-node (the real store; 31-day retention).
MemoryTSDB    - in-process store with the same query semantics, for tests and for running
                the simulator without VictoriaMetrics.

Query semantics (both stores): query_range returns points at start, start+step, ... where
each value is `agg` over the window (t - step, t].
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import math
import time
from array import array
from typing import Iterable

import httpx

log = logging.getLogger("networkguru.tsdb")

Point = tuple[str, dict, float, float]          # (metric name, labels, value, ts seconds)
AGGS = ("avg", "max", "min", "last", "p95")


def _agg(values: list[float], fn: str) -> float:
    if fn == "avg":
        return sum(values) / len(values)
    if fn == "max":
        return max(values)
    if fn == "min":
        return min(values)
    if fn == "last":
        return values[-1]
    if fn == "p95":
        s = sorted(values)
        return s[min(len(s) - 1, int(math.ceil(0.95 * len(s))) - 1)]
    raise ValueError(f"unknown aggregation {fn!r}")


def _matches(labels: tuple, match: dict) -> bool:
    d = dict(labels)
    return all(d.get(k) == v for k, v in match.items())


class MemoryTSDB:
    kind = "memory"

    def __init__(self, retention_s: float = 31 * 86400):
        self.retention_s = retention_s
        self.series: dict[tuple[str, tuple], tuple[array, array]] = {}
        self._by_name: dict[str, list[tuple[str, tuple]]] = {}
        self.points_written = 0

    def write(self, points: Iterable[Point]) -> None:
        for name, labels, value, ts in points:
            if value is None or (isinstance(value, float) and math.isnan(value)):
                continue
            key = (name, tuple(sorted(labels.items())))
            entry = self.series.get(key)
            if entry is None:
                entry = self.series[key] = (array("d"), array("d"))
                self._by_name.setdefault(name, []).append(key)
            ts_arr, v_arr = entry
            if not ts_arr or ts >= ts_arr[-1]:
                ts_arr.append(ts)
                v_arr.append(float(value))
            else:
                i = bisect.bisect_right(ts_arr, ts)
                ts_arr.insert(i, ts)
                v_arr.insert(i, float(value))
            self.points_written += 1

    async def flush(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def prune(self, now: float) -> None:
        cut = now - self.retention_s
        for ts_arr, v_arr in self.series.values():
            i = bisect.bisect_left(ts_arr, cut)
            if i:
                del ts_arr[:i]
                del v_arr[:i]

    def _keys(self, name: str, match: dict) -> list[tuple[str, tuple]]:
        return [k for k in self._by_name.get(name, ()) if _matches(k[1], match)]

    async def query_range(self, name: str, match: dict, start: float, end: float, step: float,
                          agg: str = "avg") -> list[dict]:
        out = []
        for key in self._keys(name, match):
            ts_arr, v_arr = self.series[key]
            lo = bisect.bisect_right(ts_arr, start - step)
            hi = bisect.bisect_right(ts_arr, end)
            pts: list[list[float]] = []
            bucket: list[float] = []
            n = 0                               # current bucket covers (start+(n-1)step, start+n*step]
            for i in range(lo, hi):
                t = ts_arr[i]
                k = max(0, math.ceil((t - start) / step - 1e-9))
                if k != n and bucket:
                    pts.append([start + n * step, _agg(bucket, agg)])
                    bucket = []
                n = k
                bucket.append(v_arr[i])
            if bucket and start + n * step <= end + 1e-6:
                pts.append([start + n * step, _agg(bucket, agg)])
            out.append({"labels": dict(key[1]), "points": pts})
        return out

    async def aggregate(self, name: str, match: dict, start: float, end: float, fn: str) -> float | None:
        vals: list[float] = []
        for key in self._keys(name, match):
            ts_arr, v_arr = self.series[key]
            lo = bisect.bisect_right(ts_arr, start)
            hi = bisect.bisect_right(ts_arr, end)
            vals.extend(v_arr[lo:hi])
        return _agg(vals, fn) if vals else None

    async def has_data_since(self, ts: float) -> bool:
        return any(arr[0] and arr[0][-1] >= ts for arr in self.series.values())


def _esc_tag(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ").replace("=", "\\=")


def _esc_label_value(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def line_protocol(points: Iterable[Point], measurement: str = "ng") -> list[str]:
    """Group points with equal labels+ts into one line: sh,comp=a,site=b f1=1,f2=2 <ns>."""
    lines: dict[tuple, list[str]] = {}
    for name, labels, value, ts in points:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        key = (tuple(sorted(labels.items())), int(round(ts * 1e9)))
        lines.setdefault(key, []).append(f"{_esc_tag(name)}={float(value)!r}")
    out = []
    for (labels, ns), fields in lines.items():
        tags = "".join(f",{_esc_tag(k)}={_esc_tag(v)}" for k, v in labels if v != "")
        out.append(f"{measurement}{tags} {','.join(fields)} {ns}")
    return out


class VictoriaTSDB:
    """VictoriaMetrics over HTTP. Writes are buffered and sent in the background."""
    kind = "victoriametrics"
    PREFIX = "ng_"

    def __init__(self, url: str, timeout: float = 10.0, max_buffer: int = 2_000_000):
        self.url = url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=timeout)
        self.buffer: list[str] = []
        self.max_buffer = max_buffer
        self.last_write_ok: float | None = None
        self.last_error: str | None = None
        self.points_written = 0

    def write(self, points: Iterable[Point]) -> None:
        pts = list(points)
        self.buffer.extend(line_protocol(pts))
        self.points_written += len(pts)
        if len(self.buffer) > self.max_buffer:          # VM down for a long time: drop oldest
            del self.buffer[: len(self.buffer) - self.max_buffer]

    async def flush(self) -> None:
        while self.buffer:
            chunk = self.buffer[:20000]
            try:
                r = await self.client.post(f"{self.url}/write", content="\n".join(chunk).encode())
                r.raise_for_status()
            except httpx.HTTPError as e:
                self.last_error = f"write failed: {e}"
                log.warning("VictoriaMetrics %s", self.last_error)
                return
            del self.buffer[: len(chunk)]
            self.last_write_ok = time.time()
            self.last_error = None

    async def close(self) -> None:
        await self.flush()
        await self.client.aclose()

    def prune(self, now: float) -> None:
        return None                                       # VictoriaMetrics enforces its own retention

    @classmethod
    def selector(cls, name: str, match: dict) -> str:
        inner = ",".join(f'{k}="{_esc_label_value(v)}"' for k, v in sorted(match.items()))
        return f"{cls.PREFIX}{name}{{{inner}}}"

    async def _get(self, path: str, params: dict) -> list[dict]:
        r = await self.client.get(f"{self.url}{path}", params=params)
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(body.get("error", "query failed"))
        return body["data"]["result"]

    async def query_range(self, name: str, match: dict, start: float, end: float, step: float,
                          agg: str = "avg") -> list[dict]:
        sel = self.selector(name, match)
        win = f"{int(step)}s"
        q = (f"quantile_over_time(0.95, {sel}[{win}])" if agg == "p95"
             else f"{agg}_over_time({sel}[{win}])")
        res = await self._get("/api/v1/query_range", {"query": q, "start": start, "end": end,
                                                      "step": f"{int(step)}s", "nocache": "1"})
        out = []
        for s in res:
            labels = {k: v for k, v in s["metric"].items() if k != "__name__"}
            out.append({"labels": labels, "points": [[float(t), float(v)] for t, v in s["values"]]})
        return out

    async def aggregate(self, name: str, match: dict, start: float, end: float, fn: str) -> float | None:
        sel = self.selector(name, match)
        win = f"{max(1, int(end - start))}s"
        q = {"avg": f"avg_over_time({sel}[{win}])", "max": f"max_over_time({sel}[{win}])",
             "min": f"min_over_time({sel}[{win}])", "last": f"last_over_time({sel}[{win}])",
             "p95": f"quantile_over_time(0.95, {sel}[{win}])"}[fn]
        res = await self._get("/api/v1/query", {"query": q, "time": end})
        vals = [float(s["value"][1]) for s in res]
        if not vals:
            return None
        return max(vals) if fn in ("max", "p95") else min(vals) if fn == "min" else sum(vals) / len(vals)

    async def has_data_since(self, ts: float) -> bool:
        try:
            res = await self._get("/api/v1/query", {"query": f"count(last_over_time({self.PREFIX}status[31d]))",
                                                    "time": time.time()})
        except Exception:  # noqa: BLE001 - empty / unreachable = no data
            return False
        return bool(res) and float(res[0]["value"][1]) > 0

    async def ping(self) -> bool:
        try:
            r = await self.client.get(f"{self.url}/health", timeout=3)
            return r.status_code == 200
        except httpx.HTTPError:
            return False


async def periodic_flush(tsdb, interval: float = 2.0) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await tsdb.flush()
        except Exception as e:  # noqa: BLE001
            log.warning("flush failed: %s", e)
