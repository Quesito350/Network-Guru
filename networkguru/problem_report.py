"""Admin → Report a problem: one zip with what is needed to see what went wrong.

    report.txt            version, install type, mode, uptime, health, notes, settings (no secrets),
                          which secrets are set (names only), inventory summary, the admin's note,
                          the browser details, the Mobius explorer's counts (never Mobius values) and
                          the vehicle positions' counts (never a position)
    status.json           every node's current status, reasons and last error
    events.json           7 days of status changes, system and info events (capped)
    log.txt               the last log lines (logbuffer.py)
    browser-errors.json   errors the dashboard page caught
    updates.json, morning-report.json   their public status
    inventory.yaml        the loaded inventory (addresses, but never logins - those are *_ref names)

Never: secrets.env, the admin password hash, session tokens. Every text goes through config.mask().
"""

from __future__ import annotations

import datetime as dt
import io
import json
import time
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

from . import TOOL_NAME, __version__
from .config import mask
from .model import Info

DAY = 86400.0
MAX_TEXT = 200_000           # per free-text field from the browser


def _j(obj) -> str:
    return mask(json.dumps(obj, indent=1, default=str, ensure_ascii=False))


def _clip(v, n: int = 2000) -> str:
    return str(v if v is not None else "")[:n]


def build(rt, who: str, note: str, browser: dict, secrets: list[dict], logs: str, now: float | None = None) -> tuple[str, bytes]:
    """(file name, zip bytes)."""
    now = time.time() if now is None else now
    tz = ZoneInfo(rt.inv.site.timezone)
    stamp = dt.datetime.fromtimestamp(now, tz)
    h = rt.health()
    s = rt.settings
    inv = rt.inv
    by_type: dict[str, int] = {}
    for c in inv.comps.values():
        by_type[c.type] = by_type.get(c.type, 0) + 1
    errors = [e if isinstance(e, dict) else {"message": _clip(e)} for e in (browser.get("errors") or [])][:50]
    lines = [
        f"{TOOL_NAME} problem report",
        f"Made: {stamp:%Y-%m-%d %H:%M:%S %Z} by {_clip(who, 60)}",
        "",
        "What happened (from the admin):",
        "  " + (_clip(note, MAX_TEXT).strip().replace("\n", "\n  ") or "(nothing written)"),
        "",
        f"Version: {__version__}   install: {s.deploy}   mode: {rt.mode}   uptime: {h.get('uptime_s', 0) / 3600:.1f} h",
        f"Site: {inv.site.id} ({inv.site.name}), time zone {inv.site.timezone}",
        f"Inventory: {s.inventory} - {len(inv.groups)} groups, {len(inv.comps)} components",
        "  " + ", ".join(f"{k} x{v}" for k, v in sorted(by_type.items())),
        f"History store: {h.get('tsdb')}" + (f" (error: {h.get('tsdb_error')})" if h.get("tsdb_error") else ""),
        f"Collector running: {h.get('collector_running')}   last tick: {h.get('last_tick')}   loop: {h.get('loop_ms')} ms",
        f"Health ok: {h.get('ok')}   dead tasks: {', '.join(h.get('dead_tasks') or []) or 'none'}",
        "",
        "Notes on the dashboard:",
        *([f"  - {n}{' (info)' if isinstance(n, Info) else ''}" for n in rt.notes] or ["  (none)"]),
        "",
        "Mobius explorer (counts only, never values): "
        + (", ".join(f"{k} {v}" for k, v in rt.mobius.report().items()) if getattr(rt, "mobius", None) else "not running"),
        "Vehicle positions for the map (counts only, never positions): "
        + (", ".join(f"{k} {v}" for k, v in rt.fleet.positions.counts(rt.fleet.vehicle_of).items())
           if getattr(rt, "fleet", None) else "not read (no Mobius map)"),
        "",
        "Inventory warnings:",
        *([f"  - {w}" for w in inv.warnings] or ["  (none)"]),
        "Secrets the inventory names but the server does not have: " + (", ".join(inv.missing_secrets()) or "none"),
        "",
        "Secrets (set or not - never their values):",
        *[f"  {x['label']}: {'set' if x['set'] else 'not set'} ({x['name']})" for x in secrets],
        "",
        "Settings (Admin page):",
        *[f"  {k} = {v}" for k, v in sorted(rt.settings_state.data.items())],
        "",
        "Browser:",
        f"  page: {_clip(browser.get('url'), 300)}",
        f"  browser: {_clip(browser.get('agent'), 300)}",
        f"  screen: {_clip(browser.get('screen'), 40)}   errors caught: {len(errors)}",
        "",
    ]
    files = {
        "report.txt": mask("\n".join(lines)),
        "status.json": _j(_no_positions(rt.pipeline.snapshot()) if rt.pipeline else {}),
        "events.json": _j({
            "status_changes": rt.events.events(now - 7 * DAY, now, include_suppressed=True, limit=2000) if rt.events else [],
            "system": rt.events.system_events(now - 7 * DAY, now, limit=1000) if rt.events else [],
            "info": rt.events.info_events(now - 7 * DAY, now, limit=1000) if rt.events else []}),
        "log.txt": mask(logs or "(no log lines kept)"),
        "browser-errors.json": _j([{k: _clip(v) for k, v in e.items()} for e in errors]),
        "updates.json": _j(rt.updates.public_status() if rt.updates else {}),
        "morning-report.json": _j(rt.reports.public_status() if rt.reports else {}),
    }
    try:
        files["inventory.yaml"] = mask(Path(s.inventory).read_text(encoding="utf-8"))
    except OSError as e:
        files["inventory.yaml"] = f"# could not read {s.inventory}: {e}\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
    return f"networkguru-report-{inv.site.id}-{stamp:%Y%m%d-%H%M}.zip", buf.getvalue()


def _no_positions(snapshot: dict) -> dict:
    """The live snapshot without the vehicles' GNSS positions (lat / lon info)."""
    nodes = {nid: ({**n, "i": {k: v for k, v in n["i"].items() if k not in ("lat", "lon")}}
                   if isinstance(n.get("i"), dict) else n) for nid, n in (snapshot.get("nodes") or {}).items()}
    return {**snapshot, "nodes": nodes}
