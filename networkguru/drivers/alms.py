"""Sierra ALMS (AirVantage) - for the XR80 / XR90 modems, which have no SNMP.

One account for every modem: an OAuth2 client-credentials token (an API client made in ALMS,
Develop → API clients), then `GET /api/v1/systems` at most every FETCH_EVERY_S. Each modem is
matched by the Serial column. Only signal and network values are kept - never the IMEI, IMSI,
ICCID or anything else ALMS sends. ALMS is only as fresh as the modem's own reports (minutes).
"""

from __future__ import annotations

import asyncio
import time

import httpx

from ..config import secret
from ..model import Metric
from .cellular import number

DEFAULT_URL = "https://na.airvantage.net"
DEFAULT_ID_REF, DEFAULT_SECRET_REF = "ALMS_CLIENT_ID", "ALMS_CLIENT_SECRET"
FETCH_EVERY_S = 300.0
STALE_S = 3600.0
KEEP = {   # ALMS `data` key -> (ours, kind)
    "rssi": ("cell_rssi_dbm", "dbm"), "rsrp": ("cell_rsrp_dbm", "tenths"), "rsrq": ("cell_rsrq_db", "tenths"),
    "ecio": ("ecio", "info"), "networkOperator": ("operator", "info"), "networkServiceType": ("service", "info"),
    "signalBars": ("signal_bars", "info"), "roamingStatus": ("roaming", "info"), "cellId": ("cell_id", "info"),
}


class AlmsError(Exception):
    pass


class AlmsAccount:
    """The shared ALMS login and the last list of systems (serial -> the values we keep)."""

    def __init__(self, url: str = DEFAULT_URL, id_ref: str = DEFAULT_ID_REF, secret_ref: str = DEFAULT_SECRET_REF):
        self.url = url.rstrip("/")
        self.id_ref, self.secret_ref = id_ref, secret_ref
        self.token: tuple[str, float] | None = None
        self.systems: dict[str, dict] = {}
        self.fetched = 0.0
        self.error: str | None = None
        self.lock = asyncio.Lock()

    def missing(self) -> list[str]:
        return [r for r in (self.id_ref, self.secret_ref) if not secret(r)]

    async def get(self, now: float) -> dict[str, dict]:
        async with self.lock:
            if now - self.fetched >= FETCH_EVERY_S or (self.error and now - self.fetched >= 60):
                self.fetched = now
                try:
                    self.systems = await self._fetch()
                    self.error = None
                except AlmsError as e:
                    self.error = str(e)
            if self.error:
                raise AlmsError(self.error)
            return self.systems

    async def _fetch(self) -> dict[str, dict]:
        async with httpx.AsyncClient(timeout=20.0) as c:
            try:
                if not self.token or self.token[1] < time.time() + 60:
                    r = await c.post(f"{self.url}/api/oauth/token",
                                     data={"grant_type": "client_credentials", "client_id": secret(self.id_ref),
                                           "client_secret": secret(self.secret_ref)})
                    if r.status_code in (400, 401, 403):
                        raise AlmsError(f"ALMS refused the API client ({r.status_code}) - check "
                                        f"{self.id_ref} / {self.secret_ref} and that it may use client credentials")
                    r.raise_for_status()
                    body = r.json()
                    self.token = (str(body["access_token"]), time.time() + float(body.get("expires_in") or 3600))
                out: dict[str, dict] = {}
                offset = 0
                while True:
                    r = await c.get(f"{self.url}/api/v1/systems",
                                    params={"fields": "name,gateway,lastCommDate,commStatus,data", "size": 500,
                                            "offset": offset},
                                    headers={"Authorization": f"Bearer {self.token[0]}"})
                    if r.status_code in (401, 403):
                        self.token = None
                        raise AlmsError(f"ALMS refused the system list ({r.status_code}) - the API client "
                                        "needs permission to view systems")
                    r.raise_for_status()
                    body = r.json()
                    items = body.get("items") or []
                    for s in items:
                        kept = keep(s)
                        if kept["serial"]:
                            out[kept["serial"].upper()] = kept
                    offset += len(items)
                    if not items or offset >= int(body.get("count") or 0) or offset >= 5000:
                        return out
            except AlmsError:
                raise
            except (httpx.HTTPError, ValueError, KeyError) as e:
                raise AlmsError(f"ALMS: {type(e).__name__} from {self.url}") from None


def keep(system: dict) -> dict:
    """The few values of one ALMS system we use - nothing else is kept."""
    data = system.get("data") or {}
    gw = system.get("gateway") or {}
    last = system.get("lastCommDate")
    return {"serial": str(gw.get("serialNumber") or "").strip(), "name": str(system.get("name") or "")[:80],
            "last": float(last) / 1000.0 if isinstance(last, (int, float)) and last else None,
            "comm": str(system.get("commStatus") or ""),
            "data": {k: data[k] for k in KEEP if data.get(k) not in (None, "")}}


_ACCOUNTS: dict[tuple, AlmsAccount] = {}


def account_for(comp) -> AlmsAccount:
    ex = comp.model_extra or {}
    key = (ex.get("alms_url") or DEFAULT_URL, ex.get("alms_client_id_ref") or DEFAULT_ID_REF,
           ex.get("alms_client_secret_ref") or DEFAULT_SECRET_REF)
    if key not in _ACCOUNTS:
        _ACCOUNTS[key] = AlmsAccount(*key)
    return _ACCOUNTS[key]


class AlmsReader:
    via = "alms"
    label = "ALMS (Sierra cloud)"

    def __init__(self, comp, account: AlmsAccount | None = None):
        self.comp = comp
        self.account = account or account_for(comp)

    def missing(self) -> list[str]:
        return self.account.missing()

    async def read(self, now: float) -> tuple[list[Metric], dict]:
        from .readers import ReadError
        try:
            systems = await self.account.get(now)
        except AlmsError as e:
            raise ReadError(str(e)) from None
        serial = str(self.comp.serial or "").strip().upper()
        s = systems.get(serial) if serial else None
        if s is None:
            name = (self.comp.name or "").strip().lower()
            s = next((x for x in systems.values() if name and x["name"].strip().lower() == name), None)
        if s is None:
            raise ReadError("not found in ALMS - put the modem's serial number in the Serial column"
                            if not serial else f"serial {serial} is not in this ALMS account")
        wall = time.time()
        if not s["last"] or wall - s["last"] > STALE_S:
            ago = f"{(wall - s['last']) / 3600:.0f} h ago" if s["last"] else "never"
            raise ReadError(f"ALMS: the modem's last report was {ago}")
        metrics, info = [], {}
        for k, v in s["data"].items():
            ours, how = KEEP[k]
            x = number(v)
            if how == "info":
                info[ours] = str(v)[:80]
            elif x is not None:
                # ALMS documents RSRP / RSRQ in tenths; a value past the real range is tenths
                if how == "tenths" and x < (-150 if ours == "cell_rsrp_dbm" else -25):
                    x /= 10.0
                metrics.append(Metric(ours, x))
        comm = s["comm"].upper()
        if comm in ("OK", "ERROR"):
            metrics.append(Metric("cell_connected", 1.0 if comm == "OK" else 0.0))
        info["alms_report"] = f"{(wall - s['last']) / 60:.0f} min ago"
        info["alms_status"] = s["comm"] or "?"
        return metrics, info
