"""Fake devices for the reader tests: an SNMP agent (v2c + v3 USM with SHA1/AES128), a Modbus TCP
server and an InfluxDB 1.x HTTP server, all on 127.0.0.1. Nothing here talks to a real device."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from networkguru.drivers import snmp as S


def _key(oid: str) -> tuple[int, ...]:
    return tuple(int(x) for x in oid.split("."))


def _value(tag: int, v) -> bytes:
    if tag == S.INTEGER:
        return S.enc_int(int(v))
    if tag in S.UNSIGNED:
        return S.enc_uint(int(v), tag)
    if tag == S.OCTETS:
        return S.tlv(S.OCTETS, v if isinstance(v, bytes) else str(v).encode())
    if tag == S.OID:
        return S.enc_oid(str(v))
    return S.tlv(tag, b"")


class FakeSnmpAgent(asyncio.DatagramProtocol):
    """values: {oid: (tag, value)}. v2c with `community`; v3 with `users` {name: (auth pw, priv pw)}."""

    def __init__(self, values: dict, community: str = "public", users: dict | None = None):
        self.values = values
        self.community = community.encode()
        self.users = {u.encode(): (S.localize_key(S.password_to_key(a), self.engine),
                                   S.localize_key(S.password_to_key(p), self.engine)[:16])
                      for u, (a, p) in (users or {}).items()}
        self.boots, self.t0 = 3, time.monotonic() - 1000
        self.transport = None
        self.port = 0
        self.sent: list[str] = []          # every OID this agent answered with (privacy tests)

    engine = b"\x80\x00\x1f\x88\x80fake-engine"

    async def start(self, host: str = "127.0.0.1", port: int = 0):
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=(host, port))
        self.port = self.transport.get_extra_info("sockname")[1]
        return self

    def close(self):
        if self.transport:
            self.transport.close()

    def set(self, oid: str, tag: int, value) -> None:
        self.values[oid] = (tag, value)

    # -- answers -------------------------------------------------------------------
    def _binds(self, kind: int, oids: list[str], reps: int) -> bytes:
        order = sorted(self.values, key=_key)
        out = []
        for o in oids:
            if kind == S.GET:
                tag, v = self.values.get(o, (S.NO_SUCH_INSTANCE, None))
                out.append(S.seq(S.enc_oid(o), _value(tag, v)))
                if o in self.values:
                    self.sent.append(o)
                continue
            cur = o
            for _ in range(reps if kind == S.GETBULK else 1):
                nxt = next((x for x in order if _key(x) > _key(cur)), None)
                if nxt is None:
                    out.append(S.seq(S.enc_oid(cur), S.tlv(S.END_OF_MIB, b"")))
                    break
                tag, v = self.values[nxt]
                out.append(S.seq(S.enc_oid(nxt), _value(tag, v)))
                self.sent.append(nxt)
                cur = nxt
        return S.seq(*out)

    def _answer(self, pdu_bytes: bytes) -> bytes:
        kind, rid, a, b, binds = S.parse_pdu(pdu_bytes)
        return S.tlv(S.RESPONSE, S.enc_int(rid) + S.enc_int(0) + S.enc_int(0)
                     + self._binds(kind, [o for o, _t, _v in binds], b if kind == S.GETBULK else 1))

    def datagram_received(self, data, addr):
        try:
            reply = self._reply(data)
        except S.SnmpError:
            return
        if reply:
            self.transport.sendto(reply, addr)

    def _reply(self, data: bytes) -> bytes | None:
        top = S.children(data, S.element(data, 0))
        version = S.dec_value(S.INTEGER, top[0].bytes_(data))
        if version in (0, 1):
            if top[1].bytes_(data) != self.community:
                return None                                   # a wrong community gets no answer at all
            return S.seq(S.enc_int(version), S.tlv(S.OCTETS, self.community), self._answer(data[top[2].start:top[2].b]))
        p = S.parse_v3(data)
        etime = int(time.monotonic() - self.t0)
        msg_id = S.dec_value(S.INTEGER, S.children(data, top[1])[0].bytes_(data))
        if not p.engine:                                       # discovery: report our engine
            return self._report(msg_id, "1.3.6.1.6.3.15.1.1.4.0", etime)
        if p.user not in self.users:
            return self._report(msg_id, "1.3.6.1.6.3.15.1.1.3.0", etime)
        k_auth, k_priv = self.users[p.user]
        zeroed = data[:p.auth_at] + b"\0" * 12 + data[p.auth_at + 12:]
        if hmac.new(k_auth, zeroed, hashlib.sha1).digest()[:12] != p.auth:
            return self._report(msg_id, "1.3.6.1.6.3.15.1.1.5.0", etime)
        plain = S.aes_cfb(k_priv, struct.pack(">II", p.boots, p.etime) + p.priv, p.data.bytes_(data), False)
        try:
            sc = S.children(plain, S.element(plain, 0))
            answer = self._answer(plain[sc[2].start:sc[2].b])
        except (S.SnmpError, IndexError):
            return self._report(msg_id, "1.3.6.1.6.3.15.1.1.6.0", etime)
        salt = os.urandom(8)
        scoped = S.seq(S.tlv(S.OCTETS, self.engine), S.tlv(S.OCTETS, b""), answer)
        enc = S.tlv(S.OCTETS, S.aes_cfb(k_priv, struct.pack(">II", self.boots, etime) + salt, scoped, True))
        return S.v3_message(msg_id, 0x03, self.engine, self.boots, etime, p.user, k_auth, salt, enc)

    def _report(self, msg_id: int, oid: str, etime: int) -> bytes:
        rep = S.tlv(S.REPORT, S.enc_int(0) + S.enc_int(0) + S.enc_int(0)
                    + S.seq(S.seq(S.enc_oid(oid), S.enc_uint(1, S.COUNTER32))))
        scoped = S.seq(S.tlv(S.OCTETS, self.engine), S.tlv(S.OCTETS, b""), rep)
        return S.v3_message(msg_id, 0x00, self.engine, self.boots, etime, b"", None, b"", scoped)


# -- Modbus TCP --------------------------------------------------------------------
class FakeModbus:
    """registers: {(unit, address): value 0..65535}; a missing register is exception 2."""

    def __init__(self, registers: dict):
        self.registers = registers
        self.server = None
        self.port = 0

    async def start(self, host: str = "127.0.0.1", port: int = 0):
        self.server = await asyncio.start_server(self._client, host, port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    async def _client(self, r, w):
        try:
            while True:
                head = await r.readexactly(7)
                tid, _p, length, unit = struct.unpack(">HHHB", head)
                body = await r.readexactly(length - 1)
                fn, addr, count = struct.unpack(">BHH", body[:5])
                regs = [self.registers.get((unit, a)) for a in range(addr, addr + count)]
                if fn != 3 or any(v is None for v in regs):
                    pdu = bytes([0x80 | fn, 2])
                else:
                    pdu = bytes([3, 2 * count]) + struct.pack(f">{count}H", *regs)
                w.write(struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu)
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()


# -- InfluxDB (InfluxQL; 1.x, or 3.x through its 1.x API) ---------------------------
UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}


NO_DB = "must specify a 'db' parameter, or provide the database in the InfluxQL query"


class FakeInflux:
    """measurements: {name: {"fields": {f: type}, "tags": {tag: [values]}, "points": [(time, {tag: v}, {f: v})]}}.
    Every query is kept in `queries` (and its db in `dbs`); `now() - N<unit>` windows are applied. As the real
    one, anything but SHOW DATABASES wants a database (0.9.3). `others`: more databases {name: measurements};
    `refuse(q)`: an error text for a query to refuse (InfluxDB 3's file limit), else None."""

    def __init__(self, database: str, measurements: dict, version: str = "1.8.10", others: dict | None = None,
                 refuse=None):
        self.database, self.measurements, self.version = database, measurements, version
        self.others = others or {}
        self.refuse = refuse
        self.queries: list[str] = []
        self.dbs: list[str | None] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlsplit(self.path)
                if u.path == "/ping":
                    self.send_response(204)
                    self.send_header("X-Influxdb-Version", outer.version)
                    self.end_headers()
                    return
                args = parse_qs(u.query)
                q, db = args.get("q", [""])[0], args.get("db", [None])[0]
                outer.queries.append(q)
                outer.dbs.append(db)
                meas = outer.measurements if db == outer.database else outer.others.get(db)
                error = (NO_DB if db is None and q != "SHOW DATABASES" else
                         f"database not found: {db}" if db is not None and meas is None else
                         outer.refuse(q) if outer.refuse else None)
                res = {"statement_id": 0, "error": error} if error else \
                    {"statement_id": 0, "series": outer.answer(q, meas if meas is not None else {})}
                body = json.dumps({"results": [res]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    def answer(self, q: str, measurements: dict) -> list[dict]:
        m = re.search(r'FROM "([^"]+)"', q)
        meas = measurements.get(m.group(1)) if m else None
        if q == "SHOW DATABASES":
            return [{"name": "databases", "columns": ["name"],
                     "values": [["_internal"], [self.database], *[[d] for d in self.others]]}]
        if q == "SHOW MEASUREMENTS":
            return [{"name": "measurements", "columns": ["name"], "values": [[k] for k in measurements]}]
        if q.startswith("SHOW FIELD KEYS") and meas:
            return [{"name": m.group(1), "columns": ["fieldKey", "fieldType"], "values": [[f, t] for f, t in meas["fields"].items()]}]
        if q.startswith("SHOW TAG KEYS") and meas:
            return [{"name": m.group(1), "columns": ["tagKey"], "values": [[t] for t in meas["tags"]]}]
        if q.startswith("SHOW TAG VALUES") and meas:
            key = re.search(r'WITH KEY = "([^"]+)"', q).group(1)
            return [{"name": m.group(1), "columns": ["key", "value"], "values": [[key, v] for v in meas["tags"].get(key, [])]}]
        win = re.search(r"now\(\) - (\d+)([smhd])", q)
        pts = sorted(meas["points"], key=lambda p: -p[0]) if meas else []
        if win:
            pts = [p for p in pts if p[0] > time.time() - int(win.group(1)) * UNIT_S[win.group(2)]]
        lim = re.search(r"LIMIT (\d+)", q)
        if q.startswith("SELECT *") and meas:                   # the explorer: GROUP BY * ... LIMIT n
            fields = list(meas["fields"])
            series: dict = {}
            for t, tags, vals in pts:
                key = tuple(sorted(tags.items()))
                ser = series.setdefault(key, {"name": m.group(1), "tags": dict(key), "columns": ["time", *fields], "values": []})
                if not lim or len(ser["values"]) < int(lim.group(1)):
                    ser["values"].append([t, *[vals.get(f) for f in fields]])
            return list(series.values())
        if q.startswith("SELECT") and meas:
            fields = re.findall(r'"([^"]+)"', q.split(" FROM ")[0])
            group = re.search(r'GROUP BY "([^"]+)"', q)
            pts = [p for p in pts if any(p[2].get(f) is not None for f in fields)]       # points with a selected field
            if not group:
                pts = pts[:int(lim.group(1))] if lim else pts
                return [{"name": m.group(1), "columns": ["time", *fields],
                         "values": [[t, *[vals.get(f) for f in fields]] for t, _tags, vals in pts]}] if pts else []
            out, seen = [], set()
            for t, tags, vals in pts:
                key = tags.get(group.group(1))
                if key in seen:
                    continue
                seen.add(key)
                out.append({"name": m.group(1), "tags": {group.group(1): key}, "columns": ["time", *fields],
                            "values": [[t, *[vals.get(f) for f in fields]]]})
            return out
        return []


# -- small JSON / HTTP servers (Netmaker, ALMS, the Starlink dish) -------------------
class FakeHttp:
    """routes: {(method, path): fn(handler, body) -> (status, headers, body bytes)}; every request is kept."""

    def __init__(self, routes: dict, host: str = "127.0.0.1", port: int = 0):
        self.routes, self.requests = routes, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                path = urlsplit(self.path).path
                outer.requests.append({"method": method, "path": self.path, "headers": dict(self.headers), "body": body})
                fn = outer.routes.get((method, path))
                status, headers, out = fn(self, body) if fn else (404, {}, b"not found")
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

        self.server = ThreadingHTTPServer((host, port), H)
        self.port = self.server.server_address[1]
        self.url = f"http://{host}:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def json_reply(obj, status: int = 200):
    return lambda _h, _b: (status, {"Content-Type": "application/json"}, json.dumps(obj).encode())
