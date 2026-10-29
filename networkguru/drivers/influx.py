"""InfluxDB over HTTP, InfluxQL (1.x, and 3.x through its 1.x API) - the Mobius server's database - and
`probe-influx`.

The probe lists the layout only - databases, measurements, field keys and types, tag keys, the
values of asset-like tags (vehicle names; never user names) and each measurement's newest time -
never a data value. Its output is what the Mobius map (the Mobius map sheet) is written from.
InfluxDB 3 refuses queries over all time (too many files), so the newest time and the tag values are
asked for the last PROBE_WINDOW only, and one failing query is noted instead of ending the probe.
"""

from __future__ import annotations

import datetime as dt
import re

import httpx

ASSET_LIKE = re.compile(r"asset|machine|vehicle|unit|name|host|device|id$", re.IGNORECASE)
PEOPLE_LIKE = re.compile(r"user|login|account|mail|finger|person|operator", re.IGNORECASE)
PROBE_WINDOW = "7d"


class InfluxError(Exception):
    pass


class InfluxClient:
    def __init__(self, url: str, database: str | None = None, user: str | None = None,
                 password: str | None = None, timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.database = database
        self.auth = (user, password) if user and password else None
        self.http = httpx.AsyncClient(timeout=timeout)
        self.version: str | None = None                      # from /ping

    @property
    def v3(self) -> bool:
        return str(self.version or "").startswith("3")

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
        self.version = r.headers.get("X-Influxdb-Version")
        return self.version or "?"

    async def query(self, q: str, db: str | None = None, timeout: float | None = None) -> list[dict]:
        """The series of the first statement: [{"name", "tags", "columns", "values"}]. `timeout`: this query's own."""
        params = {"q": q, "epoch": "s"}
        if db or self.database:
            params["db"] = db or self.database
        try:
            r = await self.http.get(f"{self.url}/query", params=params, auth=self.auth,
                                    timeout=timeout if timeout else httpx.USE_CLIENT_DEFAULT)
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


_FOUND: dict[str, str] = {}          # InfluxDB URL -> the database find_database picked


async def find_database(c: InfluxClient, tables=()) -> str:
    """The database to read, also set on the client: the configured one, else the one of SHOW DATABASES
    (without _internal) that holds the most of `tables` - a Mobius InfluxDB can have several (0.9.3).
    Remembered per URL until forget_database()."""
    if c.database:
        return c.database
    if c.url in _FOUND:
        c.database = _FOUND[c.url]
        return c.database
    dbs = [v[0] for s in await c.query("SHOW DATABASES") for v in s.get("values") or [] if v and v[0] != "_internal"]
    if not dbs:
        raise InfluxError("no database on this InfluxDB")
    want = set(tables)
    best, error = dbs[0], None
    if len(dbs) > 1 and want:
        scores = []
        for d in dbs:
            series, err = await _try(c, "SHOW MEASUREMENTS", d)
            error = error or err
            scores.append(len(want & {v[0] for s in series for v in s.get("values") or []}))
        if not max(scores):
            raise InfluxError(error or f"none of the Mobius map's tables is in any database ({', '.join(dbs)})")
        best = dbs[scores.index(max(scores))]
    _FOUND[c.url] = c.database = best
    return best


def forget_database(url: str | None) -> None:
    """After an InfluxDB error: pick the database again next time."""
    _FOUND.pop(str(url or "").rstrip("/"), None)


def q_ident(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def q_str(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


async def _try(c: InfluxClient, q: str, db: str) -> tuple[list[dict], str | None]:
    try:
        return await c.query(q, db), None
    except InfluxError as e:
        return [], str(e)[:160]


async def probe(c: InfluxClient, db: str | None = None, max_measurements: int = 150, max_tag_values: int = 40) -> str:
    """The layout of the database(s) as text - names only, no data values."""
    lines = [f"InfluxDB at {c.url} - version {await c.ping()}",
             f"(layout only: names, types and newest times of the last {PROBE_WINDOW}; no data values)", ""]
    recent = f"time > now() - {PROBE_WINDOW}"
    dbs = [db] if db else [v[0] for s in await c.query("SHOW DATABASES") for v in s.get("values") or []
                           if v and v[0] != "_internal"]
    for d in dbs:
        lines.append(f"database {d}")
        series, err = await _try(c, "SHOW MEASUREMENTS", d)
        if err:
            lines.append(f"  (measurements: {err})")
        ms = [v[0] for s in series for v in s.get("values") or []]
        for m in ms[:max_measurements]:
            fs, ferr = await _try(c, f"SHOW FIELD KEYS FROM {q_ident(m)}", d)
            fields = [(v[0], v[1]) for s in fs for v in s.get("values") or []]
            ts, terr = await _try(c, f"SHOW TAG KEYS FROM {q_ident(m)}", d)
            tags = [v[0] for s in ts for v in s.get("values") or []]
            newest = "?"
            numeric = next((f for f, t in fields if t in ("float", "integer")), fields[0][0] if fields else None)
            if numeric:
                rows, nerr = await _try(c, f"SELECT {q_ident(numeric)} FROM {q_ident(m)} WHERE {recent} "
                                           "ORDER BY time DESC LIMIT 1", d)
                t = rows[0]["values"][0][0] if rows and rows[0].get("values") else None
                newest = (dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ") if t
                          else f"error: {nerr}" if nerr else f"none in the last {PROBE_WINDOW}")
            lines.append(f"  measurement {m}   (newest point {newest})")
            lines.append("    fields: " + (", ".join(f"{f} ({t})" for f, t in fields) or (f"- ({ferr})" if ferr else "-")))
            lines.append("    tags:   " + (", ".join(tags) or (f"- ({terr})" if terr else "-")))
            for k in tags:
                if not ASSET_LIKE.search(k) or PEOPLE_LIKE.search(k):
                    continue
                where = f" WHERE {recent}" if c.v3 else ""
                vs, verr = await _try(c, f"SHOW TAG VALUES FROM {q_ident(m)} WITH KEY = {q_ident(k)}{where}", d)
                vals = [v[1] for s in vs for v in s.get("values") or []]
                more = f" (+{len(vals) - max_tag_values} more)" if len(vals) > max_tag_values else ""
                lines.append(f"    {k} values: " + (", ".join(map(str, vals[:max_tag_values])) + more if vals
                                                    else f"- ({verr})" if verr else "-"))
        if len(ms) > max_measurements:
            lines.append(f"  (+{len(ms) - max_measurements} more measurements)")
        lines.append("")
    return "\n".join(lines)
