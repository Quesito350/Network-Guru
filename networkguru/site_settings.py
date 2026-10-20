"""The admin's settings (admin page): times of the daily jobs and where the morning report goes.

Nothing secret lives here. The webhook URL, mail passwords and the GitHub token are only in
the server's secrets.env (typed during install.sh).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .daily import valid_hhmm
from .state import StateFile

DEFAULTS: dict[str, Any] = {
    "update_check_time": "06:00",
    "report_time": "06:30",
    "report_teams": True,
    "report_email": True,
    "email_to": [],
    "email_from": "",
    "email_via": "graph",            # graph (Microsoft 365) | smtp
    "graph_tenant": "",
    "graph_client_id": "",
    "graph_sender": "",
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_security": "starttls",     # starttls | ssl | none (none = relay, no sign-in)
    "dashboard_url": "",
}
_EMAIL = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_GUID = re.compile(r"^[0-9a-fA-F-]{36}$|^[A-Za-z0-9.-]{3,253}$")


def open_settings(data_dir: Path, public_url: str = "") -> StateFile:
    defaults = dict(DEFAULTS, dashboard_url=public_url or "")
    return StateFile(Path(data_dir) / "state" / "settings.json", defaults)


def validate(patch: dict[str, Any]) -> dict[str, Any]:
    """Checked, normalised values from the admin page. Raises ValueError with a readable message."""
    out: dict[str, Any] = {}
    for key, value in patch.items():
        if key not in DEFAULTS:
            raise ValueError(f"unknown setting '{key}'")
        if key in ("update_check_time", "report_time"):
            if not valid_hhmm(str(value)):
                raise ValueError(f"{key}: use HH:MM, 24-hour (for example 06:30)")
            out[key] = str(value)
        elif key in ("report_teams", "report_email"):
            out[key] = bool(value)
        elif key == "email_to":
            items = value if isinstance(value, list) else re.split(r"[,;\s]+", str(value or ""))
            items = [i.strip() for i in items if i and i.strip()]
            bad = [i for i in items if not _EMAIL.match(i)]
            if bad or len(items) > 50:
                raise ValueError(f"email_to: not an e-mail address: {', '.join(bad[:3])}" if bad else
                                 "email_to: at most 50 addresses")
            out[key] = items
        elif key in ("email_from", "graph_sender"):
            v = str(value or "").strip()
            if v and not _EMAIL.match(v):
                raise ValueError(f"{key}: not an e-mail address")
            out[key] = v
        elif key == "email_via":
            if value not in ("graph", "smtp"):
                raise ValueError("email_via: graph or smtp")
            out[key] = value
        elif key == "smtp_security":
            if value not in ("starttls", "ssl", "none"):
                raise ValueError("smtp_security: starttls, ssl or none")
            out[key] = value
        elif key == "smtp_port":
            try:
                port = int(value)
            except (TypeError, ValueError):
                raise ValueError("smtp_port: a number") from None
            if not 1 <= port <= 65535:
                raise ValueError("smtp_port: 1-65535")
            out[key] = port
        elif key == "smtp_host":
            v = str(value or "").strip()
            if v and not _HOST.match(v):
                raise ValueError("smtp_host: a host name or IP address")
            out[key] = v
        elif key in ("graph_tenant", "graph_client_id"):
            v = str(value or "").strip()
            if v and not _GUID.match(v):
                raise ValueError(f"{key}: an ID like 00000000-0000-0000-0000-000000000000")
            out[key] = v
        elif key == "dashboard_url":
            v = str(value or "").strip()
            if v and not re.match(r"^https?://[^\s<>\"]+$", v):
                raise ValueError("dashboard_url: starts with http:// or https://")
            out[key] = v
    return out
