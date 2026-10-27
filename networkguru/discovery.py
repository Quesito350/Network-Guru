"""The discovery scan (0.9.0): find the site's devices on the network and follow the ones that moved.

Where: the /24 of every private address in the inventory, plus `discovery.ranges` (the Sites sheet
Subnets). Private ranges only, at most MAX_ADDRESSES per scan, about RATE probes a second.

1. Who answers: one ping each (when ICMP is allowed) and a TCP connect to the Mobius ports
   (18080 Mobius, 8086 InfluxDB) on every address - Windows usually blocks ping.
2. What it is, asked only of hosts that answered:
     Mobius server      port 18080 open
     InfluxDB           GET /ping -> X-Influxdb-Version
     NTRIP caster       GET / -> SOURCETABLE 200 OK (a GNSS base)
     Cerbo GX           Modbus unit 100; register 800 = its serial (the MAC, as text)
     Windows PCs        a NetBIOS name query (UDP 137): computer name + MAC; else reverse DNS
     SNMP devices       first a v3 discovery request with NO login; only agents that answer it get
                        the site's own logins (the ones the inventory names) - never guessed ones.
                        sysObjectID's enterprise number gives the type; the vendor serial the identity.
Nothing on any device is changed; only reads.

Matching (match()): known (identity or address matches an inventory device - identity the inventory
lacks is learned), moved (identity proven, other address: followed by itself), changed (an inventory
address now answers as another type), new (waits for an admin: Add or Ignore).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import struct
import time
from dataclasses import asdict, dataclass, field

import httpx

from .config import secret
from .drivers import snmp as S
from .drivers.modbus import ModbusClient, ModbusError
from .drivers.reach import ping, tcp_check

log = logging.getLogger("networkguru.discovery")

PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
MAX_ADDRESSES = 65536
RATE = 300.0                 # probes started per second
TIMEOUT = 1.0
SCAN_LIMIT_S = 1800.0
SYS_DESCR, SYS_OBJECT, SYS_NAME = "1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.2.0", "1.3.6.1.2.1.1.5.0"
ENTERPRISE = {34861: "rajant", 57344: "tachyon", 14988: "mikrotik", 20542: "cell_modem", 16177: "westermo",
              41112: "airmax"}
VENDOR = {34861: "Rajant", 57344: "Tachyon", 14988: "MikroTik", 20542: "Sierra Wireless", 16177: "Westermo",
          41112: "Ubiquiti", 311: "Microsoft", 8072: "Net-SNMP", 9: "Cisco", 11: "HP", 4526: "Netgear"}
SERIAL_OIDS = {"rajant": ["1.3.6.1.4.1.34861.1.1", "1.3.6.1.4.1.34861.1.1.0"],
               "tachyon": ["1.3.6.1.4.1.57344.1.3.2.0"], "mikrotik": ["1.3.6.1.4.1.14988.1.1.7.3.0"]}
MT_LTE_RSRP = "1.3.6.1.4.1.14988.1.1.16.1.1.4"
ROLE_LABEL = {"mobius_server": "Mobius server", "influxdb": "InfluxDB", "gnss_base": "NTRIP caster (GNSS base)",
              "victron_cerbo": "Cerbo GX", "rajant": "Rajant", "tachyon": "Tachyon", "mikrotik": "MikroTik router",
              "cell_modem": "cell modem", "westermo": "Westermo", "airmax": "Ubiquiti airMAX", "switch": "switch",
              "host": "network device"}


class ScanError(ValueError):
    pass


@dataclass(frozen=True)
class Ports:
    mobius: int = 18080
    influx: int = 8086
    ntrip: int = 2101
    modbus: int = 502
    snmp: int = 161
    netbios: int = 137
    extra: tuple[int, ...] = (22, 80, 443)


@dataclass
class Found:
    """One host that answered, and what it was told to be."""
    address: str
    roles: list[str] = field(default_factory=list)
    how: list[str] = field(default_factory=list)
    name: str = ""
    serial: str = ""
    mac: str = ""
    model: str = ""
    ports: list[int] = field(default_factory=list)


# -- where to scan -------------------------------------------------------------------------
def _ip(text: str | None):
    try:
        return ipaddress.ip_address((text or "").strip().split(":")[0])
    except ValueError:
        return None


def is_private(net, allow_loopback: bool = False) -> bool:
    if allow_loopback and net.subnet_of(ipaddress.ip_network("127.0.0.0/8")):
        return True
    return any(net.version == p.version and net.subnet_of(p) for p in PRIVATE)


def ranges(inv, extra: list[str] | tuple = (), allow_loopback: bool = False, around: bool = True) -> list:
    """The networks to scan: the /24 around each inventory address + discovery.ranges + extra."""
    nets = set()
    if around:
        for c in inv.comps.values():
            for h in (c.host, (c.target or "").rpartition(":")[0] if c.target else None):
                ip = _ip(h)
                if ip and ip.version == 4:
                    n = ipaddress.ip_network(f"{ip}/24", strict=False)
                    if is_private(n, allow_loopback):
                        nets.add(n)
    for r in list(getattr(inv.data, "discovery").ranges) + list(extra):
        try:
            n = ipaddress.ip_network(str(r).strip(), strict=False)
        except ValueError:
            raise ScanError(f"'{r}' is not a network (use e.g. 10.200.4.0/24)") from None
        if n.version != 4 or not is_private(n, allow_loopback):
            raise ScanError(f"{n} is not a private network - only 10.x, 172.16-31.x and 192.168.x are scanned")
        nets.add(n)
    out = list(ipaddress.collapse_addresses(sorted(nets)))
    total = sum(n.num_addresses for n in out)
    if total > MAX_ADDRESSES:
        raise ScanError(f"{total} addresses is more than one scan does ({MAX_ADDRESSES}) - use smaller Subnets")
    return out


def addresses(nets) -> list[str]:
    out = []
    for n in nets:
        out.extend(str(a) for a in (n.hosts() if n.num_addresses > 2 else n))
    return out


# -- NetBIOS (UDP 137): computer name and MAC of a Windows PC -------------------------------
def netbios_request(tid: int) -> bytes:
    """An NBSTAT query for '*' (RFC 1002)."""
    return (struct.pack(">HHHHHH", tid & 0xFFFF, 0, 1, 0, 0, 0) + b"\x20" + b"CK" + b"A" * 30 + b"\x00"
            + struct.pack(">HH", 0x21, 0x01))


def parse_netbios(data: bytes) -> tuple[str, str] | None:
    """(computer name, MAC) from an NBSTAT answer, or None."""
    try:
        i = 12
        if data[i] & 0xC0 == 0xC0:
            i += 2
        else:
            while data[i]:
                i += 1 + data[i]
            i += 1
        rtype, _rclass, _ttl, _rdlen = struct.unpack(">HHIH", data[i:i + 10])
        i += 10
        if rtype != 0x21:
            return None
        count = data[i]
        i += 1
        name = ""
        for _ in range(count):
            raw, suffix, flags = data[i:i + 15], data[i + 15], struct.unpack(">H", data[i + 16:i + 18])[0]
            i += 18
            if not name and suffix == 0x00 and not flags & 0x8000:        # the workstation name, not a group
                name = raw.decode("ascii", "replace").strip()
        mac = data[i:i + 6]
        mac_s = ":".join(f"{b:02x}" for b in mac) if len(mac) == 6 and any(mac) else ""
        return (name, mac_s) if name or mac_s else None
    except (IndexError, struct.error):
        return None


class _Udp(asyncio.DatagramProtocol):
    def __init__(self):
        self.fut: asyncio.Future | None = None

    def datagram_received(self, data, addr):
        if self.fut and not self.fut.done():
            self.fut.set_result(data)

    def error_received(self, exc):
        if self.fut and not self.fut.done():
            self.fut.set_exception(exc)


async def udp_ask(host: str, port: int, packet: bytes, timeout: float) -> bytes | None:
    loop = asyncio.get_running_loop()
    try:
        transport, proto = await loop.create_datagram_endpoint(_Udp, remote_addr=(host, port))
    except OSError:
        return None
    try:
        proto.fut = loop.create_future()
        transport.sendto(packet)
        return await asyncio.wait_for(proto.fut, timeout)
    except (TimeoutError, asyncio.TimeoutError, OSError):
        return None
    finally:
        transport.close()


# -- the site's own SNMP logins (by the names the inventory uses) --------------------------
def site_logins(inv) -> list[dict]:
    """Every SNMP login the inventory names whose value is set - v3 users first. Values stay in memory."""
    from .importers.build import CELL_DEFAULTS, TYPE_DEFAULTS
    seen, out = set(), []
    refsets = [dict(TYPE_DEFAULTS.get(t, {})) for t in ("rajant", "tachyon", "switch")] + [dict(CELL_DEFAULTS["snmp"])]
    for c in inv.comps.values():
        ex = c.model_extra or {}
        if any(k.startswith("snmp_") for k in ex):
            refsets.append({k: v for k, v in ex.items() if k.startswith("snmp_")})
    for r in refsets:
        if str(r.get("snmp_version")) == "3":
            vals = tuple(secret(r.get(k) or "") for k in ("snmp_user_ref", "snmp_auth_ref", "snmp_priv_ref"))
            if all(vals) and ("3",) + vals not in seen:
                seen.add(("3",) + vals)
                out.insert(0, {"version": "3", "user": vals[0], "auth": vals[1], "priv": vals[2],
                               "name": r.get("snmp_user_ref")})
        else:
            v = secret(r.get("snmp_community_ref") or "")
            if v and ("2c", v) not in seen:
                seen.add(("2c", v))
                out.append({"version": "2c", "community": v, "name": r.get("snmp_community_ref")})
    return out


def _text(v) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace").strip().strip("\0")[:120]
    return "" if v is None else str(v)[:120]


# -- the scanner -----------------------------------------------------------------------------
class Scanner:
    def __init__(self, logins: list[dict], can_ping: bool = True, ports: Ports = Ports(), rate: float = RATE,
                 timeout: float = TIMEOUT, concurrency: int = 200):
        self.logins, self.can_ping, self.ports = logins, can_ping, ports
        self.rate, self.timeout = rate, timeout
        self.sem = asyncio.Semaphore(concurrency)
        self.progress = {"phase": "starting", "done": 0, "total": 0}
        self.login_sent_to: set[str] = set()          # hosts that were given a login (tests check this)
        self._next = 0.0

    async def _pace(self) -> None:
        now = time.monotonic()
        wait = self._next - now
        self._next = max(now, self._next) + 1.0 / self.rate
        if wait > 0:
            await asyncio.sleep(wait)

    async def run(self, nets) -> tuple[list[Found], int]:
        """(hosts that answered with what they are, how many addresses were tried)."""
        addrs = addresses(nets)
        self.progress = {"phase": "who answers", "done": 0, "total": len(addrs)}
        alive: dict[str, set[int]] = {}

        async def probe(a: str):
            async with self.sem:
                await self._pace()
                ok, open_ = await self._alive(a)
                self.progress["done"] += 1
                if ok:
                    alive[a] = open_
        await asyncio.gather(*(probe(a) for a in addrs))
        self.progress = {"phase": "what they are", "done": 0, "total": len(alive)}
        found: list[Found] = []

        async def ident(a: str, open_: set[int]):
            async with self.sem:
                try:
                    found.append(await self._identify(a, open_))
                except Exception:  # noqa: BLE001 - one odd device must not stop the scan
                    log.exception("discovery: identifying %s failed", a)
                    found.append(Found(a))
                self.progress["done"] += 1
        await asyncio.gather(*(ident(a, o) for a, o in alive.items()))
        found.sort(key=lambda f: tuple(int(x) for x in f.address.split(".")))
        return found, len(addrs)

    async def _alive(self, a: str) -> tuple[bool, set[int]]:
        jobs = [tcp_check(a, p, self.timeout) for p in (self.ports.mobius, self.ports.influx)]
        if self.can_ping:
            jobs.append(ping(a, 1))
        got = await asyncio.gather(*jobs, return_exceptions=True)
        open_ = {p for p, r in zip((self.ports.mobius, self.ports.influx), got[:2])
                 if not isinstance(r, BaseException) and r[0] == "open"}
        answered = any(not isinstance(r, BaseException) and r[0] in ("open", "refused") for r in got[:2])
        if self.can_ping and not isinstance(got[-1], BaseException) and got[-1]:
            answered = True
        return answered, open_

    async def _identify(self, a: str, open_: set[int]) -> Found:
        f = Found(a)
        more = [self.ports.ntrip, self.ports.modbus, *self.ports.extra]
        states = await asyncio.gather(*(tcp_check(a, p, self.timeout) for p in more))
        open_ |= {p for p, (st, _ms) in zip(more, states) if st == "open"}
        f.ports = sorted(open_)
        if self.ports.mobius in open_:
            f.roles.append("mobius_server")
            f.how.append(f"port {self.ports.mobius} open (Mobius)")
        if self.ports.influx in open_:
            v = await self._influx(a)
            if v is not None:
                f.roles.append("influxdb")
                f.how.append(f"InfluxDB {v} on port {self.ports.influx}")
        if self.ports.ntrip in open_ and await self._ntrip(a):
            f.roles.append("gnss_base")
            f.how.append(f"NTRIP caster on port {self.ports.ntrip}")
        if self.ports.modbus in open_:
            await self._cerbo(a, f)
        await self._snmp(a, f)
        nb = parse_netbios(await udp_ask(a, self.ports.netbios, netbios_request(0x4E47), self.timeout) or b"")
        if nb:
            f.name = f.name or nb[0]
            f.mac = f.mac or nb[1]
            f.how.append("NetBIOS name")
        if f.roles and not f.name:
            f.name = await self._rdns(a)
        return f

    async def _influx(self, a: str) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout + 1) as c:
                r = await c.get(f"http://{a}:{self.ports.influx}/ping")
        except httpx.HTTPError:
            return None
        return r.headers.get("X-Influxdb-Version") if r.status_code in (200, 204) else None

    async def _ntrip(self, a: str) -> bool:
        try:
            rd, wr = await asyncio.wait_for(asyncio.open_connection(a, self.ports.ntrip), self.timeout)
        except (OSError, TimeoutError, asyncio.TimeoutError):
            return False
        try:
            wr.write(b"GET / HTTP/1.0\r\nUser-Agent: NTRIP NetworkGuru\r\nNtrip-Version: Ntrip/2.0\r\n\r\n")
            await wr.drain()
            head = await asyncio.wait_for(rd.read(512), self.timeout + 1)
        except (OSError, TimeoutError, asyncio.TimeoutError):
            return False
        finally:
            wr.close()
        return b"SOURCETABLE 200" in head or b"gnss/sourcetable" in head.lower()

    async def _cerbo(self, a: str, f: Found) -> None:
        try:
            async with ModbusClient(a, self.ports.modbus, timeout=self.timeout + 1) as m:
                regs = await m.read_holding(100, 800, 6)            # com.victronenergy.system "Serial (System)"
        except ModbusError:
            return
        text = b"".join(struct.pack(">H", r) for r in regs).decode("ascii", "replace").strip("\0 ")
        f.roles.append("victron_cerbo")
        f.how.append("Modbus unit 100 (Victron system)")
        if re.fullmatch(r"[0-9a-fA-F]{12}", text):
            f.serial = text.lower()
            f.mac = f.mac or ":".join(text.lower()[i:i + 2] for i in range(0, 12, 2))

    async def _snmp(self, a: str, f: Found) -> None:
        if not await S.engine_probe(a, self.ports.snmp, self.timeout):
            return                                            # no SNMP agent: no login is sent
        if not self.logins:
            f.how.append("SNMP agent (no site login set to identify it)")
            return
        for login in self.logins:
            try:
                c = S.SnmpClient(a, self.ports.snmp, version=login["version"], community=login.get("community"),
                                 user=login.get("user"), auth_password=login.get("auth"),
                                 priv_password=login.get("priv"), timeout=self.timeout, retries=0)
            except S.SnmpError:
                continue
            self.login_sent_to.add(a)
            try:
                got = await c.get([SYS_DESCR, SYS_OBJECT, SYS_NAME])
                if not got:
                    continue
                await self._snmp_facts(c, got, f)
                return
            except S.SnmpError:
                continue
            finally:
                c.close()
        f.how.append("SNMP agent (none of the site's logins fit)")

    async def _snmp_facts(self, c, got: dict, f: Found) -> None:
        oid = str(got.get(SYS_OBJECT) or "")
        parts = oid.split(".")
        ent = int(parts[6]) if len(parts) > 6 and oid.startswith("1.3.6.1.4.1.") and parts[6].isdigit() else None
        descr = _text(got.get(SYS_DESCR))
        f.name = _text(got.get(SYS_NAME)) or f.name
        vendor = VENDOR.get(ent, "")
        # the vendor in front, so a cell modem's Model picks its reader (cellular.kind)
        f.model = (descr if not vendor or vendor.split()[0].lower() in descr.lower() else f"{vendor} {descr}")[:80]
        role = ENTERPRISE.get(ent) if ent is not None else None
        if role == "mikrotik":
            if await c.walk(MT_LTE_RSRP, max_rows=1, bulk=1):
                f.roles.append("cell_modem")
                f.how.append("SNMP: MikroTik with an LTE modem")
        if not role:
            role = "switch" if re.search(r"switch", descr, re.IGNORECASE) else "host"
        if role in SERIAL_OIDS:
            serial = await c.get(SERIAL_OIDS[role])
            f.serial = next((_text(v) for v in serial.values() if _text(v)), "") or f.serial
        if role not in f.roles:
            f.roles.append(role)
        f.how.append(f"SNMP: {VENDOR.get(ent, 'enterprise ' + str(ent) if ent else 'agent')}")

    async def _rdns(self, a: str) -> str:
        loop = asyncio.get_running_loop()
        try:
            host, _ = await asyncio.wait_for(loop.getnameinfo((a, 0), socket.NI_NAMEREQD), 2.0)
        except (OSError, TimeoutError, asyncio.TimeoutError, socket.gaierror):
            return ""
        return host.split(".")[0] if host and host != a else ""


# -- matching ----------------------------------------------------------------------------------
def norm_name(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def norm_mac(s: str | None) -> str:
    h = re.sub(r"[^0-9a-f]", "", (s or "").lower())
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else ""


def item_key(role: str, f: Found) -> str:
    who = f.serial or norm_mac(f.mac) or norm_name(f.name) or f.address
    return f"{role}:{who}"


def match(found: list[Found], inv, learned: dict | None = None) -> list[dict]:
    """One item per (host, role): {"key", "address", "type", "how", "name", "serial", "mac", "model",
    "ports", "outcome": known | moved | changed | new, "comp", "old"} - and identity to learn ("learn")."""
    learned = learned or {}
    by_serial, by_mac, by_name, by_addr = {}, {}, {}, {}
    for c in inv.comps.values():
        lr = learned.get(c.id, {})
        for s in (c.serial, lr.get("serial")):
            if s:
                by_serial.setdefault(str(s).strip().lower(), c.id)
        for m in (c.mac, lr.get("mac")):
            if norm_mac(m):
                by_mac.setdefault(norm_mac(m), c.id)
        for n in (c.hostname, lr.get("name")):
            if norm_name(n):
                by_name.setdefault(norm_name(n), c.id)
        ip = _ip(c.host)
        if ip:
            by_addr.setdefault(str(ip), []).append(c.id)
    items = []
    for f in found:
        for role in f.roles:
            it = {"key": item_key(role, f), "address": f.address, "type": role,
                  "how": [h for h in f.how], "name": f.name, "serial": f.serial, "mac": norm_mac(f.mac),
                  "model": f.model, "ports": f.ports, "outcome": "new", "comp": None, "old": None, "learn": None}
            cid = None
            for table, val in ((by_serial, f.serial.strip().lower()), (by_mac, norm_mac(f.mac)), (by_name, norm_name(f.name))):
                if val and val in table and _same_kind(inv.comps[table[val]].type, role):
                    cid = table[val]
                    break
            if cid:
                cur = str(_ip(inv.comps[cid].host) or "")
                it["comp"] = cid
                if cur == f.address:
                    it["outcome"] = "known"
                else:
                    it["outcome"], it["old"] = "moved", cur or None
            else:
                here = by_addr.get(f.address, [])
                same = next((x for x in here if _same_kind(inv.comps[x].type, role)), None)
                if same:
                    it["outcome"], it["comp"] = "known", same
                elif here and role not in ("host",) and _snmp_typed(f, role) and \
                        all(inv.comps[x].type in ENTERPRISE.values() or inv.comps[x].type == "switch" for x in here):
                    it["outcome"], it["comp"] = "changed", here[0]
                elif here and role == "host":
                    it["outcome"], it["comp"] = "known", here[0]
            if it["outcome"] == "known" and it["comp"]:
                c = inv.comps[it["comp"]]
                lr = learned.get(c.id, {})
                learn = {k: v for k, v in (("serial", f.serial), ("mac", norm_mac(f.mac)), ("name", f.name))
                         if v and not getattr(c, {"name": "hostname"}.get(k, k), None) and not lr.get(k)}
                it["learn"] = learn or None
            items.append(it)
    return items


def _same_kind(inv_type: str, role: str) -> bool:
    if inv_type == role:
        return True
    pairs = {("mikrotik", "cell_modem"), ("cell_modem", "mikrotik"), ("host", "switch"), ("switch", "host")}
    return (inv_type, role) in pairs or inv_type == "host" or role == "host"


def _snmp_typed(f: Found, role: str) -> bool:
    return any(h.startswith("SNMP:") for h in f.how) and role in ENTERPRISE.values()


def found_dict(f: Found) -> dict:
    return asdict(f)
