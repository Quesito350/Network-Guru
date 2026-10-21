"""InfluxDB 1.x over HTTP (the Mobius server's database) and `probe-influx`.

The probe lists the layout only - databases, measurements, field keys and types, tag keys, the
values of asset-like tags (vehicle names) and each measurement's newest time - never a data value.
Its output is what the Mobius mapping (mobius_map.py) is written from.
"""

from __future__ import annotations

import datetime as dt
import re

import httpx

ASSET_LIKE = re.compile(r"asset|machine|vehicle|unit|name|host|device|id$", re.IGNORECASE)


class InfluxError(Exception):
    pass


class InfluxClient:
    def __init__(self, url: str, database: str | None = None, user: str | None = None,
                 password: str | None = None, timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.database = database
        self.auth = (user, password) if user and password else None
        self.http = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.http.aclose()

    async def ping(self) -> str:
        """The server's version, or InfluxError."""
        try:
            r = await self.http.get(f"{self.url}/ping", auth=self.auth)
        except httpx.HTTPError as e:
            raise InfluxError(f"cannot reach {self.url} ({type(e).__name__})") from None
        if r.status_code not in (200, 204):
            raise InfluxError(f"{self.url}/ping answered {r.status_code}")
        return r.headers.get("X-Influxdb-Version", "?")

    async def query(self, q: str, db: str | None = None) -> list[dict]:
        """The series of the first statement: [{"name", "tags", "columns", "values"}]."""
        params = {"q": q, "epoch": "s"}
        if db or self.database:
            params["db"] = db or self.database
        try:
            r = await self.http.get(f"{self.url}/query", params=params, auth=self.auth)
        except httpx.HTTPError as e:
            raise InfluxError(f"cannot reach {self.url} ({type(e).__name__})") from None
        if r.status_code == 401:
            raise InfluxError("InfluxDB wants a user and password (set the mobius user_ref / password_ref)")
        try:
            body = r.json()
        except ValueError:
            raise InfluxError(f"InfluxDB answered {r.status_code} without JSON") from None
        if "error" in body:
            raise InfluxError(f"InfluxDB: {body['error']}")
        res = (body.get("results") or [{}])[0]
        if "error" in res:
            raise InfluxError(f"InfluxDB: {res['error']}")
        return res.get("series") or []


def q_ident(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def q_str(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


async def probe(c: InfluxClient, db: str | None = None, max_measurements: int = 150, max_tag_values: int = 40) -> str:
    """The layout of the database(s) as text - names only, no data values."""
    lines = [f"InfluxDB at {c.url} - version {await c.ping()}", "(layout only: names, types and newest times; no data values)", ""]
    dbs = [db] if db else [v[0] for s in await c.query("SHOW DATABASES") for v in s.get("values") or []
                           if v and v[0] != "_internal"]
    for d in dbs:
        lines.append(f"database {d}")
        ms = [v[0] for s in await c.query("SHOW MEASUREMENTS", d) for v in s.get("values") or []]
        for m in ms[:max_measurements]:
            fields = [(v[0], v[1]) for s in await c.query(f"SHOW FIELD KEYS FROM {q_ident(m)}", d) for v in s.get("values") or []]
            tags = [v[0] for s in await c.query(f"SHOW TAG KEYS FROM {q_ident(m)}", d) for v in s.get("values") or []]
            newest = "?"
            numeric = next((f for f, t in fields if t in ("float", "integer")), fields[0][0] if fields else None)
            if numeric:
                rows = await c.query(f"SELECT {q_ident(numeric)} FROM {q_ident(m)} ORDER BY time DESC LIMIT 1", d)
                t = rows[0]["values"][0][0] if rows and rows[0].get("values") else None
                newest = dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ") if t else "never"
            lines.append(f"  measurement {m}   (newest point {newest})")
            lines.append("    fields: " + (", ".join(f"{f} ({t})" for f, t in fields) or "-"))
            lines.append("    tags:   " + (", ".join(tags) or "-"))
            for k in tags:
                if not ASSET_LIKE.search(k):
                    continue
                vals = [v[1] for s in await c.query(f"SHOW TAG VALUES FROM {q_ident(m)} WITH KEY = {q_ident(k)}", d)
                        for v in s.get("values") or []]
                more = f" (+{len(vals) - max_tag_values} more)" if len(vals) > max_tag_values else ""
                lines.append(f"    {k} values: " + ", ".join(map(str, vals[:max_tag_values])) + more)
        if len(ms) > max_measurements:
            lines.append(f"  (+{len(ms) - max_measurements} more measurements)")
        lines.append("")
    return "\n".join(lines)
