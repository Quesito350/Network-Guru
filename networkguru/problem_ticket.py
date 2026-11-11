"""Report a problem → a network change request (0.10.0).

Anyone viewing the dashboard can report a problem. The server keeps the full problem report (the admin's
zip, problem_report.py) under data/reports/, and hands back a ticket for the site's Network Change
Requests page (a Claude page - Admin → Settings → Change request page): the page opens with the ticket
in its link's #ng= part (base64url JSON, the contract below), filled in, with an error-log excerpt; the
person picks their team and presses Submit there, signed in as themselves. Network Guru can't save a
ticket by itself: the page's requests live in a Claude page's own database.

    #ng= + base64url(UTF-8 JSON, no padding):
    {"v": 1, "from": "network-guru", "title", "type": "Other", "site": NCR location, "priority",
     "description", "justification", "devices" (optional), "log" (<= EXCERPT_MAX, masked),
     "ngReport": "NG-YYMMDD-XXXX", "ngVersion", "ngSite"}

The excerpt: version, site, health, the event loop's stalls, the dashboard's notes, the page's caught
errors and the last warning / error log lines - every text through config.mask(). Never the inventory,
statuses, positions or names of people.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import secrets as _secrets
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import TOOL_NAME, __version__
from .config import mask
from .model import Info

NCR_LOCATIONS = ("Mendon", "Tremonton", "Logan", "Dallas/Fort Worth", "Denver", "Other")
PRIORITIES = ("Standard", "High", "Urgent", "Emergency")
EXCERPT_MAX = 8000           # characters of error log in the ticket
LOG_LINES = 60               # the last warning / error lines
TEXT_MAX = 4000              # what the person writes
KEEP_DAYS = 30
KEEP_MAX = 50
PER_HOUR = 10                # reports per address
ID_RE = re.compile(r"^NG-\d{6}-[A-Z0-9]{4}$")
_LEVEL = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ (WARNING|ERROR|CRITICAL) ")


def location_for(setting: str, site_name: str, site_id: str) -> str:
    """The NCR page's Location: the admin's choice, else guessed from the site's name."""
    if setting in NCR_LOCATIONS:
        return setting
    text = f"{site_name} {site_id}".lower()
    for loc in NCR_LOCATIONS[:-1]:
        if loc.lower() in text or (loc == "Dallas/Fort Worth" and ("dallas" in text or "dfw" in text)):
            return loc
    return "Other"


def clean_text(text: str) -> str:
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text or "")).strip()
    if not t:
        raise ValueError("Say what's wrong first.")
    if len(t) > TEXT_MAX:
        raise ValueError(f"That's too long - at most {TEXT_MAX} characters.")
    return t


def warn_lines(entries: list[str], n: int = LOG_LINES, each: int = 1500) -> list[str]:
    """The last `n` warning / error entries of the log buffer (a traceback stays with its line)."""
    out = [e[:each] for e in entries if _LEVEL.match(e)]
    return out[-n:]


def new_id(now: float, tz: ZoneInfo) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return f"NG-{dt.datetime.fromtimestamp(now, tz):%y%m%d}-" + "".join(_secrets.choice(alphabet) for _ in range(4))


def excerpt(rt, rid: str, page: str, browser: dict, entries: list[str], now: float) -> str:
    """The error log that goes in the ticket, at most EXCERPT_MAX characters, masked."""
    tz = ZoneInfo(rt.inv.site.timezone)
    h = rt.health()
    head = [
        f"{TOOL_NAME} {__version__} - site {rt.inv.site.id} ({rt.inv.site.name}) - report {rid}",
        f"Made: {dt.datetime.fromtimestamp(now, tz):%Y-%m-%d %H:%M %Z} - page {page or '(unknown)'}",
        f"Health ok: {h.get('ok')} - dead tasks: {', '.join(h.get('dead_tasks') or []) or 'none'} - "
        f"uptime {h.get('uptime_s', 0) / 3600:.1f} h - mode {rt.mode}",
        f"Event loop stalls (last 10 min): {h.get('stalls_250ms')} over 250 ms, longest {h.get('stall_max_ms')} ms",
        "Notes on the dashboard:",
        *([f"  - {str(n)[:300]}{' (info)' if isinstance(n, Info) else ''}" for n in rt.notes] or ["  (none)"]),
    ]
    errors = [e for e in (browser.get("errors") or []) if isinstance(e, dict)][:10]
    head.append(f"Errors this page caught: {len(browser.get('errors') or [])}")
    head += [f"  - {str(e.get('message', ''))[:300]} {str(e.get('source', ''))[:120]}".rstrip() for e in errors]
    head.append("Log (the last warnings and errors):")
    lines = warn_lines(entries)
    text = mask("\n".join(head + (lines or ["  (none kept)"])))
    while len(text) > EXCERPT_MAX and lines:          # the oldest log lines go first
        lines = lines[1:]
        text = mask("\n".join(head + ["  (older lines left out)"] + lines))
    return text[:EXCERPT_MAX]


def device_of(rt, page: str) -> str:
    """The device a #/c/<id> page shows: "<group> · <name> (<address>)"."""
    m = re.match(r"^#/c/([^?/]+)", page or "")
    c = rt.inv.comps.get(m.group(1)) if m else None
    if c is None:
        return ""
    gid = rt.inv.comp_group.get(c.id, "")
    g = rt.inv.groups.get(gid)
    name = f"{getattr(g, 'name', '') or gid} · {c.name or c.id}"
    return f"{name} ({c.host.split(':')[0]})" if c.host else name


def ticket(rt, rid: str, text: str, priority: str, page: str, location: str, log: str) -> dict:
    first = text.splitlines()[0].strip()[:90]
    where = location if location != "Other" else rt.inv.site.name
    t = {
        "v": 1, "from": "network-guru",
        "title": f"{TOOL_NAME} ({where}): {first}"[:200],
        "type": "Other", "site": location, "priority": priority,
        "description": text + (f"\n\nPage: {page}" if page else ""),
        "justification": f"Reported from {TOOL_NAME} at {rt.inv.site.name} (report {rid}). The dashboard's error log "
                         f"is attached; the full report is on the {TOOL_NAME} server (Admin → Problem reports).",
        "log": log, "ngReport": rid, "ngVersion": __version__, "ngSite": rt.inv.site.name,
    }
    dev = device_of(rt, page)
    if dev:
        t["devices"] = dev
    return t


def encode(t: dict) -> str:
    raw = json.dumps(t, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(value: str) -> dict:
    v = value.split("ng=", 1)[-1].strip()
    return json.loads(base64.urlsafe_b64decode(v + "=" * (-len(v) % 4)).decode("utf-8"))


class ReportStore:
    """The kept problem reports: data/reports/<id>.zip and an index (newest first)."""

    def __init__(self, data_dir: Path, clock=time.time):
        self.dir = Path(data_dir) / "reports"
        self.index_path = self.dir / "index.json"
        self.clock = clock

    def _index(self) -> list[dict]:
        try:
            items = json.loads(self.index_path.read_text(encoding="utf-8"))
            return [x for x in items if isinstance(x, dict) and ID_RE.match(str(x.get("id", "")))]
        except (OSError, ValueError):
            return []

    def _write(self, items: list[dict]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8", newline="\n")
        tmp.replace(self.index_path)

    def save(self, rid: str, data: bytes, meta: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{rid}.zip").write_bytes(data)
        items = [dict(meta, id=rid, size=len(data))] + [x for x in self._index() if x["id"] != rid]
        self._write(self._prune(items))

    def _prune(self, items: list[dict]) -> list[dict]:
        now = self.clock()
        keep = [x for x in items if now - float(x.get("t", 0)) <= KEEP_DAYS * 86400][:KEEP_MAX]
        ids = {x["id"] for x in keep}
        for p in self.dir.glob("NG-*.zip"):
            if p.stem not in ids:
                try:
                    p.unlink()
                except OSError:
                    pass
        return keep

    def list(self) -> list[dict]:
        return self._index()

    def path(self, rid: str) -> Path | None:
        if not ID_RE.match(rid or ""):
            return None
        p = self.dir / f"{rid}.zip"
        return p if p.is_file() and any(x["id"] == rid for x in self._index()) else None
