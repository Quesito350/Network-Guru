"""A small async SNMP client: GET and GETBULK walks, v2c and v3 (USM, RFC 3414 / 3826).

    v2c   community string
    v3    authPriv with HMAC-SHA1-96 authentication and AES-128-CFB privacy - what the Rajant
          change request uses. Engine discovery, key localisation and the time window are handled
          here; the v3 report counters become readable errors ("wrong auth password", ...).

Only `cryptography` (AES) beyond the standard library. Values come back as int (INTEGER, Counter,
Gauge, TimeTicks, Counter64), bytes (OCTET STRING), str (OID, IpAddress) or None (NULL,
noSuchObject, noSuchInstance, endOfMibView). Credentials are passed in by the caller from the
secrets file - never logged, never in an error text.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import struct
import time
from dataclasses import dataclass

INTEGER, OCTETS, NULL, OID = 0x02, 0x04, 0x05, 0x06
SEQ = 0x30
IPADDR, COUNTER32, GAUGE32, TIMETICKS, OPAQUE, COUNTER64 = 0x40, 0x41, 0x42, 0x43, 0x44, 0x46
NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB = 0x80, 0x81, 0x82
GET, GETNEXT, RESPONSE, GETBULK, REPORT = 0xA0, 0xA1, 0xA2, 0xA5, 0xA8
UNSIGNED = (COUNTER32, GAUGE32, TIMETICKS, COUNTER64)
MISSING = (NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB)

USM_ERRORS = {   # usmStats* counters in a v3 report
    "1.3.6.1.6.3.15.1.1.1.0": "the device does not accept this security level (it needs authPriv)",
    "1.3.6.1.6.3.15.1.1.2.0": "not in the SNMP v3 time window",
    "1.3.6.1.6.3.15.1.1.3.0": "unknown SNMP v3 user",
    "1.3.6.1.6.3.15.1.1.4.0": "unknown SNMP engine ID",
    "1.3.6.1.6.3.15.1.1.5.0": "wrong SNMP v3 auth password",
    "1.3.6.1.6.3.15.1.1.6.0": "wrong SNMP v3 privacy password",
}
NOT_IN_TIME = "1.3.6.1.6.3.15.1.1.2.0"
ERROR_STATUS = {1: "response too big", 2: "no such name", 3: "bad value", 5: "general error",
                6: "no access", 16: "authorization error"}


class SnmpError(Exception):
    """A readable reason, never a secret."""


# -- BER -----------------------------------------------------------------------
def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _len(len(content)) + content


def enc_int(v: int, tag: int = INTEGER) -> bytes:
    return tlv(tag, v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big", signed=True))


def enc_uint(v: int, tag: int) -> bytes:
    b = v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big")
    return tlv(tag, (b"\0" + b) if b[0] & 0x80 else b)


def enc_oid(oid: str) -> bytes:
    parts = [int(p) for p in oid.strip(".").split(".")]
    out = bytearray([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(reversed(chunk))
    return tlv(OID, bytes(out))


def seq(*items: bytes) -> bytes:
    return tlv(SEQ, b"".join(items))


@dataclass(frozen=True)
class El:
    """One BER element in a buffer: its tag, where it starts, and its content [a, b)."""
    tag: int
    start: int
    a: int
    b: int

    def bytes_(self, data: bytes) -> bytes:
        return bytes(data[self.a:self.b])


def element(data: bytes, i: int) -> El:
    if i + 2 > len(data):
        raise SnmpError("truncated SNMP message")
    tag, n, j = data[i], data[i + 1], i + 2
    if n & 0x80:
        k = n & 0x7F
        if k == 0 or k > 4 or j + k > len(data):
            raise SnmpError("bad SNMP length")
        n = int.from_bytes(data[j:j + k], "big")
        j += k
    if j + n > len(data):
        raise SnmpError("truncated SNMP message")
    return El(tag, i, j, j + n)


def children(data: bytes, parent: El) -> list[El]:
    out, i = [], parent.a
    while i < parent.b:
        e = element(data, i)
        out.append(e)
        i = e.b
    return out


def dec_oid(b: bytes) -> str:
    if not b:
        return ""
    parts = [b[0] // 40, b[0] % 40]
    v = 0
    for x in b[1:]:
        v = (v << 7) | (x & 0x7F)
        if not x & 0x80:
            parts.append(v)
            v = 0
    return ".".join(map(str, parts))


def dec_value(tag: int, b: bytes):
    if tag == INTEGER:
        return int.from_bytes(b, "big", signed=True) if b else 0
    if tag in UNSIGNED:
        return int.from_bytes(b, "big") if b else 0
    if tag in (OCTETS, OPAQUE):
        return bytes(b)
    if tag == OID:
        return dec_oid(b)
    if tag == IPADDR:
        return ".".join(str(x) for x in b)
    return None                                  # NULL, noSuchObject, noSuchInstance, endOfMibView


def pdu(kind: int, req_id: int, oids: list[str], a: int = 0, b: int = 0) -> bytes:
    """GET / GETNEXT (a, b = error status / index = 0) or GETBULK (a = non-repeaters, b = max-repetitions)."""
    binds = b"".join(seq(enc_oid(o), tlv(NULL, b"")) for o in oids)
    return tlv(kind, enc_int(req_id) + enc_int(a) + enc_int(b) + seq(binds))


def parse_pdu(data: bytes, at: int = 0) -> tuple[int, int, int, int, list[tuple[str, int, object]]]:
    """(pdu tag, request id, error status, error index, [(oid, value tag, value)])."""
    p = element(data, at)
    f = children(data, p)
    if len(f) != 4:
        raise SnmpError("bad SNMP PDU")
    req, err, idx = (dec_value(INTEGER, x.bytes_(data)) for x in f[:3])
    binds = []
    for vb in children(data, f[3]):
        kv = children(data, vb)
        if len(kv) == 2:
            binds.append((dec_oid(kv[0].bytes_(data)), kv[1].tag, dec_value(kv[1].tag, kv[1].bytes_(data))))
    return p.tag, req, err, idx, binds


# -- USM keys and crypto (RFC 3414 A.2.2, RFC 3826) ------------------------------
def password_to_key(password: str) -> bytes:
    pw = password.encode("utf-8")
    if not pw:
        raise SnmpError("empty SNMP v3 password")
    return hashlib.sha1((pw * (1048576 // len(pw) + 1))[:1048576]).digest()


def localize_key(key: bytes, engine_id: bytes) -> bytes:
    return hashlib.sha1(key + engine_id + key).digest()


def aes_cfb(key16: bytes, iv: bytes, data: bytes, encrypt: bool) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    c = Cipher(algorithms.AES(key16), modes.CFB(iv))
    op = c.encryptor() if encrypt else c.decryptor()
    return op.update(data) + op.finalize()


def security_params(engine: bytes, boots: int, etime: int, user: bytes, auth: bytes, priv: bytes) -> tuple[bytes, int]:
    """The USM parameters as BER, and where the 12 auth bytes are inside them."""
    pre = tlv(OCTETS, engine) + enc_int(boots) + enc_int(etime) + tlv(OCTETS, user)
    body = pre + tlv(OCTETS, auth) + tlv(OCTETS, priv)
    out = tlv(SEQ, body)
    return out, (len(out) - len(body)) + len(pre) + 2


def v3_message(msg_id: int, flags: int, engine: bytes, boots: int, etime: int, user: bytes,
               auth_key: bytes | None, priv: bytes, data: bytes) -> bytes:
    """A whole v3 message; with an auth key, its HMAC-SHA1-96 is filled in."""
    head = enc_int(3) + seq(enc_int(msg_id), enc_int(65507), tlv(OCTETS, bytes([flags])), enc_int(3))
    sp, at_in_sp = security_params(engine, boots, etime, user, b"\0" * 12 if auth_key else b"", priv)
    sp_wrapped = tlv(OCTETS, sp)
    body = head + sp_wrapped + data
    msg = tlv(SEQ, body)
    if not auth_key:
        return msg
    at = (len(msg) - len(body)) + len(head) + (len(sp_wrapped) - len(sp)) + at_in_sp
    mac = hmac.new(auth_key, msg, hashlib.sha1).digest()[:12]
    return msg[:at] + mac + msg[at + 12:]


@dataclass
class V3Parts:
    flags: int
    engine: bytes
    boots: int
    etime: int
    user: bytes
    auth: bytes
    priv: bytes
    auth_at: int              # where the 12 auth bytes are in the whole message
    data: El                  # the scoped PDU, or the encrypted one (an OCTET STRING)


def parse_v3(raw: bytes) -> V3Parts:
    top = children(raw, element(raw, 0))
    if len(top) != 4 or dec_value(INTEGER, top[0].bytes_(raw)) != 3:
        raise SnmpError("bad SNMP v3 answer")
    glob = children(raw, top[1])
    flags = raw[glob[2].a] if len(glob) == 4 and glob[2].b > glob[2].a else 0
    sp = children(raw, element(raw, top[2].a))
    if len(sp) != 6:
        raise SnmpError("bad SNMP v3 security parameters")
    g = [x.bytes_(raw) for x in sp]
    return V3Parts(flags, g[0], dec_value(INTEGER, g[1]), dec_value(INTEGER, g[2]), g[3], g[4], g[5], sp[4].a, top[3])


# -- transport -------------------------------------------------------------------
class _Proto(asyncio.DatagramProtocol):
    def __init__(self):
        self.waiting: asyncio.Future | None = None

    def datagram_received(self, data, addr):
        if self.waiting and not self.waiting.done():
            self.waiting.set_result(data)

    def error_received(self, exc):
        if self.waiting and not self.waiting.done():
            self.waiting.set_exception(exc)


def _rand31() -> int:
    return int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF


class SnmpClient:
    """One device. `async with SnmpClient(...) as c:` (or call close())."""

    def __init__(self, host: str, port: int = 161, *, version: str = "2c", community: str | None = None,
                 user: str | None = None, auth_password: str | None = None, priv_password: str | None = None,
                 timeout: float = 1.5, retries: int = 1):
        self.host, self.port, self.version = host, port, str(version)
        self.timeout, self.retries = timeout, retries
        self.community = (community or "").encode("utf-8")
        self.user = (user or "").encode("utf-8")
        if self.version == "3":
            if not user:
                raise SnmpError("no SNMP v3 user name")
            for what, v in (("auth", auth_password), ("privacy", priv_password)):
                if not v or len(v) < 8:
                    raise SnmpError(f"the SNMP v3 {what} password must be at least 8 characters")
            self._ku_auth = password_to_key(auth_password)
            self._ku_priv = password_to_key(priv_password)
        elif self.version not in ("2c", "1"):
            raise SnmpError(f"SNMP version {version} is not supported (use 2c or 3)")
        elif not community:
            raise SnmpError("no SNMP community")
        self.engine_id = b""
        self.boots = 0
        self._time0 = 0
        self._time_at = 0.0
        self._k_auth = self._k_priv = b""
        self._salt = int.from_bytes(os.urandom(8), "big")
        self._transport = None
        self._proto: _Proto | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.close()

    def close(self):
        if self._transport:
            self._transport.close()
            self._transport = None

    async def _exchange(self, packet: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        if self._transport is None:
            try:
                self._transport, self._proto = await loop.create_datagram_endpoint(
                    _Proto, remote_addr=(self.host, self.port))
            except OSError as e:
                raise SnmpError(f"cannot reach {self.host} ({e.strerror or e})") from None
        last = "timeout"
        for _ in range(self.retries + 1):
            fut = loop.create_future()
            self._proto.waiting = fut
            self._transport.sendto(packet)
            try:
                return await asyncio.wait_for(fut, self.timeout)
            except (TimeoutError, asyncio.TimeoutError):
                last = "timeout"
            except OSError as e:
                last = f"network error: {e.strerror or e}"
        raise SnmpError(f"no SNMP answer ({last})")

    # -- requests ----------------------------------------------------------------
    async def request(self, kind: int, oids: list[str], a: int = 0, b: int = 0) -> list[tuple[str, int, object]]:
        async with self._lock:
            req_id = _rand31()
            body = pdu(kind, req_id, oids, a, b)
            if self.version == "3":
                return await self._v3(body, req_id)
            raw = await self._exchange(seq(enc_int(1 if self.version == "2c" else 0), tlv(OCTETS, self.community), body))
            top = children(raw, element(raw, 0))
            if len(top) != 3:
                raise SnmpError("bad SNMP answer")
            return _checked(parse_pdu(raw, top[2].start), req_id)

    async def get(self, oids: list[str]) -> dict[str, object]:
        return {o: v for o, t, v in await self.request(GET, oids) if t not in MISSING}

    async def walk(self, base: str, max_rows: int = 1000, bulk: int = 20) -> list[tuple[str, object]]:
        """Every (oid, value) under `base`, with GETBULK (GETNEXT for v1)."""
        base = base.strip(".")
        out, cur = [], base
        while len(out) < max_rows:
            if self.version == "1":
                binds = await self.request(GETNEXT, [cur])
            else:
                binds = await self.request(GETBULK, [cur], 0, bulk)
            if not binds:
                return out
            for oid, t, v in binds:
                if t == END_OF_MIB or not oid.startswith(base + ".") or oid == cur:
                    return out
                out.append((oid, v))
                cur = oid
        return out

    # -- v3 ----------------------------------------------------------------------
    def _engine_time(self) -> int:
        return int(self._time0 + (time.monotonic() - self._time_at))

    async def _discover(self) -> None:
        msg_id = _rand31()
        scoped = seq(tlv(OCTETS, b""), tlv(OCTETS, b""), pdu(GET, msg_id, []))
        raw = await self._exchange(v3_message(msg_id, 0x04, b"", 0, 0, b"", None, b"", scoped))
        p = parse_v3(raw)
        if not p.engine:
            raise SnmpError("the device did not report its SNMP engine ID")
        self.engine_id, self.boots = p.engine, p.boots
        self._time0, self._time_at = p.etime, time.monotonic()
        self._k_auth = localize_key(self._ku_auth, p.engine)
        self._k_priv = localize_key(self._ku_priv, p.engine)[:16]

    def _request(self, body: bytes) -> bytes:
        boots, etime = self.boots, self._engine_time()
        self._salt = (self._salt + 1) & 0xFFFFFFFFFFFFFFFF
        salt = self._salt.to_bytes(8, "big")
        scoped = seq(tlv(OCTETS, self.engine_id), tlv(OCTETS, b""), body)
        enc = tlv(OCTETS, aes_cfb(self._k_priv, struct.pack(">II", boots, etime) + salt, scoped, True))
        return v3_message(_rand31(), 0x07, self.engine_id, boots, etime, self.user, self._k_auth, salt, enc)

    async def _v3(self, body: bytes, req_id: int):
        if not self.engine_id:
            await self._discover()
        for attempt in range(2):
            raw = await self._exchange(self._request(body))
            p = parse_v3(raw)
            if p.flags & 0x01:                                  # an authenticated answer: check it
                zeroed = raw[:p.auth_at] + b"\0" * 12 + raw[p.auth_at + 12:]
                if not hmac.compare_digest(hmac.new(self._k_auth, zeroed, hashlib.sha1).digest()[:12], p.auth):
                    raise SnmpError("an SNMP v3 answer failed its authentication check")
            if p.flags & 0x02:
                plain = aes_cfb(self._k_priv, struct.pack(">II", p.boots, p.etime) + p.priv, p.data.bytes_(raw), False)
            else:
                plain = raw[p.data.start:p.data.b]
            try:
                sc = children(plain, element(plain, 0))
                parsed = parse_pdu(plain, sc[2].start)
            except (SnmpError, IndexError):
                raise SnmpError("wrong SNMP v3 privacy password (the answer did not decrypt)") from None
            tag, _rid, _e, _i, binds = parsed
            if tag == REPORT and binds and binds[0][0] == NOT_IN_TIME and attempt == 0:
                self.boots, self._time0, self._time_at = p.boots, p.etime, time.monotonic()   # resync once
                continue
            return _checked(parsed, req_id)
        raise SnmpError("not in the SNMP v3 time window")


def _checked(parsed, req_id: int):
    tag, rid, err, idx, binds = parsed
    if tag == REPORT:
        raise SnmpError(next((USM_ERRORS[o] for o, _t, _v in binds if o in USM_ERRORS),
                             "the device answered with an SNMP report"))
    if tag != RESPONSE or rid != req_id:
        raise SnmpError("unexpected SNMP answer")
    if err:
        raise SnmpError(f"SNMP error: {ERROR_STATUS.get(err, err)} (item {idx})")
    return binds
