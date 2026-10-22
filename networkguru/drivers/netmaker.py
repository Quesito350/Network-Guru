"""WireGuard tunnels, from the Netmaker gateway (a cloud Linux server) - its REST API, GET only.

    /api/hosts     one per machine: name, endpoint, version, OS
    /api/nodes     one per host and network: connected (the admin's switch), last check-in, status
    /api/metrics   Pro servers only: latency and uptime per peer (a 403 / 404 here is skipped)

Every vehicle / location with a "Netmaker host" (Groups sheet) has a `wireguard` component, matched
by that host name. Netmaker marks a node offline after 10 minutes without a check-in. The gateway
itself is a `netmaker` component (ping + port 443) whose reader counts the nodes and lists the
hosts that are not in the inventory. Keys (public keys, the token) are never kept or shown.
"""

from __future__ import annotations

import asyncio
import re
import time

import httpx

from ..config import secret
from ..model import CompResult, Metric

FETCH_EVERY_S = 20.0
OFFLINE_AFTER_S = 600.0
DEFAULT_TOKEN_REF = "NETMAKER_TOKEN"


class NetmakerError(Exception):
    pass


def norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


class NetmakerApi:
    """The shared login and the last node list (one fetch serves the poller and the gateway card)."""

    def __init__(self, url: str, token_ref: str = DEFAULT_TOKEN_REF):
        self.url = url.rstrip("/")
        self.token_ref = token_ref
        self.nodes: list[dict] = []
        self.fetched = -1e18
        self.error: str | None = None
        self.metrics_off_until = 0.0
        self.lock = asyncio.Lock()

    def missing(self) -> list[str]:
        return [] if secret(self.token_ref) else [self.token_ref]

    async def get(self, now: float) -> list[dict]:
        async with self.lock:
            if now - self.fetched >= FETCH_EVERY_S:
                self.fetched = now
                try:
                    self.nodes = await self._fetch(now)
                    self.error = None
                except NetmakerError as e:
                    self.error = str(e)
            if self.error:
                raise NetmakerError(self.error)
            return self.nodes

    async def _fetch(self, now: float) -> list[dict]:
        headers = {"Authorization": f"Bearer {secret(self.token_ref)}"}
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as c:
            hosts = await self._json(c, "/api/hosts")
            nodes = await self._json(c, "/api/nodes")
            metrics = {}
            if now >= self.metrics_off_until:
                try:
                    metrics = await self._json(c, "/api/metrics")
                except NetmakerError:
                    self.metrics_off_until = now + 3600          # Community Edition: no metrics
        if not isinstance(hosts, list) or not isinstance(nodes, list):
            raise NetmakerError("Netmaker answered, but not with host and node lists")
        by_host = {h.get("id"): h for h in hosts if isinstance(h, dict)}
        per_node = metrics.get("nodes", metrics) if isinstance(metrics, dict) else {}
        out = []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            h = by_host.get(n.get("hostid")) or {}
            out.append(_node(n, h, per_node.get(n.get("id")) if isinstance(per_node, dict) else None))
        return out

    async def _json(self, c: httpx.AsyncClient, path: str):
        try:
            r = await c.get(self.url + path)
        except httpx.HTTPError as e:
            raise NetmakerError(f"cannot reach Netmaker at {self.url} ({type(e).__name__})") from None
        if r.status_code in (401, 403):
            raise NetmakerError(f"Netmaker refused {path} ({r.status_code}) - check {self.token_ref}")
        if r.status_code != 200:
            raise NetmakerError(f"Netmaker {path} answered {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise NetmakerError(f"Netmaker {path} did not answer with JSON") from None


def _node(n: dict, h: dict, m) -> dict:
    """The few values of one node we keep (no keys)."""
    node = {"id": str(n.get("id") or ""), "host": str(h.get("name") or "")[:80], "network": str(n.get("network") or ""),
            "address": str(n.get("address") or "").split("/")[0], "connected": n.get("connected", True) is not False,
            "checkin": float(n["lastcheckin"]) if isinstance(n.get("lastcheckin"), (int, float)) and n["lastcheckin"] > 0 else None,
            "status": str(n.get("status") or "").lower(), "endpoint": str(h.get("endpointip") or ""),
            "version": str(h.get("version") or ""), "os": str(h.get("os") or ""),
            "roles": ", ".join(r for r, k in (("relay", "isrelay"), ("relayed", "isrelayed"),
                                              ("egress gateway", "isegressgateway"),
                                              ("ingress gateway", "isingressgateway")) if n.get(k))}
    conn = (m or {}).get("connectivity") if isinstance(m, dict) else None
    if isinstance(conn, dict):
        lat = [p.get("latency") for p in conn.values() if isinstance(p, dict) and p.get("connected")
               and isinstance(p.get("latency"), (int, float)) and 0 <= p["latency"] < 999]
        up = [p.get("percentup") for p in conn.values() if isinstance(p, dict) and isinstance(p.get("percentup"), (int, float))]
        node["latency"] = min(lat) if lat else None
        node["uptime_pct"] = max(up) if up else None
    return node


def online(node: dict, wall: float) -> bool:
    if not node["connected"] or node["status"] in ("offline", "disconnected"):
        return False
    return node["checkin"] is not None and wall - node["checkin"] < OFFLINE_AFTER_S


def _ago(s: float) -> str:
    return f"{s / 3600:.0f} h ago" if s >= 5400 else f"{s / 60:.0f} min ago" if s >= 90 else f"{s:.0f} s ago"


_APIS: dict[tuple[str, str], NetmakerApi] = {}


def api_for(cfg) -> NetmakerApi | None:
    if not cfg or not cfg.url:
        return None
    key = (cfg.url.rstrip("/"), cfg.token_ref or DEFAULT_TOKEN_REF)
    if key not in _APIS:
        _APIS[key] = NetmakerApi(*key)
    return _APIS[key]


def host_of(comp, inv) -> str:
    ex = comp.model_extra or {}
    return str(ex.get("netmaker_host") or comp.hostname or inv.groups[inv.comp_group[comp.id]].name)


class NetmakerPoller:
    """Every `wireguard` component, from one node list."""

    def __init__(self, inv, comps: list, api: NetmakerApi, interval: float = 30.0):
        self.inv, self.api = inv, api
        self.comps = comps
        self.comp_ids = [c.id for c in comps]
        self.interval = interval
        self.timeout = 40.0

    async def poll(self, now: float) -> list[CompResult]:
        try:
            nodes = await self.api.get(now)
        except NetmakerError as e:
            return [CompResult(cid, now, False, error=str(e)) for cid in self.comp_ids]
        wall = time.time()
        out = []
        for c in self.comps:
            want = norm(host_of(c, self.inv))
            net = (c.model_extra or {}).get("netmaker_network")
            mine = [n for n in nodes if norm(n["host"]) == want and (not net or n["network"] == net)]
            if not mine:
                out.append(CompResult(c.id, now, False, error=f"no Netmaker host named '{host_of(c, self.inv)}' "
                                                               "- check the Netmaker host column"))
                continue
            n = max(mine, key=lambda x: (online(x, wall), x["checkin"] or 0))
            info = {k: v for k, v in (("netmaker_network", n["network"]), ("vpn_address", n["address"]),
                                      ("endpoint", n["endpoint"]), ("netclient", n["version"]), ("os", n["os"]),
                                      ("netmaker_role", n["roles"]), ("netmaker_status", n["status"])) if v}
            if not online(n, wall):
                why = ("switched off in Netmaker (disconnected)" if not n["connected"] else
                       f"Netmaker: last check-in {_ago(wall - n['checkin'])}" if n["checkin"] else
                       "Netmaker: never checked in")
                out.append(CompResult(c.id, now, False, info=info, error=why))
                continue
            metrics = [Metric("wg_checkin_age_s", round(max(0.0, wall - n["checkin"]))),
                       Metric("read_ok", 1.0, {"via": "netmaker"})]
            if n.get("latency") is not None:
                metrics.append(Metric("wg_latency_ms", float(n["latency"])))
            if n.get("uptime_pct") is not None:
                metrics.append(Metric("wg_up_pct", float(n["uptime_pct"])))
            out.append(CompResult(c.id, now, True, metrics, info))
        return out


class NetmakerReader:
    """The gateway card: nodes online / total and the hosts that are not in the inventory."""
    via = "netmaker"
    label = "Netmaker API"

    def __init__(self, comp, inv, api: NetmakerApi):
        self.comp, self.inv, self.api = comp, inv, api
        self.known = {norm(host_of(c, inv)) for c in inv.comps.values() if c.type == "wireguard"}

    def missing(self) -> list[str]:
        return self.api.missing()

    async def read(self, now: float) -> tuple[list[Metric], dict]:
        from .readers import ReadError
        try:
            nodes = await self.api.get(now)
        except NetmakerError as e:
            raise ReadError(str(e)) from None
        wall = time.time()
        up = sum(1 for n in nodes if online(n, wall))
        extra = sorted({n["host"] for n in nodes if n["host"] and norm(n["host"]) not in self.known})
        info = {"not_in_inventory": (", ".join(extra[:15]) + (f" (+{len(extra) - 15} more)" if len(extra) > 15 else ""))
                if extra else "none"}
        return [Metric("netmaker_nodes", float(len(nodes))), Metric("netmaker_nodes_online", float(up))], info
