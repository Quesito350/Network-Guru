"""Settings and secrets.

Secrets come from the environment (development) or from the server's secrets.env, which
install.sh writes with hidden input (mode 600) and compose mounts read-only at
/run/networkguru/. Code asks for them BY NAME with secret(); every value handed out is
remembered and masked out of logs, errors and API answers.

secrets.env is plain KEY=VALUE, one per line, read by this module (never by compose), so
`$` and quotes in a value need no escaping. The file is re-read when it changes.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

_SECRETS: set[str] = set()
_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
DEFAULT_SECRETS_FILE = "/run/networkguru/secrets.env"
# Webhook URLs carry their key in the query string (sig=...); never show those.
_URL_SECRET = re.compile(r"(?i)\b(sig|code|key)=[\w%\-.~]{12,}")


class _SecretsFile:
    def __init__(self):
        self.lock = threading.Lock()
        self.path: str | None = None
        self.mtime: float | None = None
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        path = os.environ.get("NETWORKGURU_SECRETS") or (DEFAULT_SECRETS_FILE if os.name != "nt" else "")
        if not path:
            return None
        with self.lock:
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                self.path, self.mtime, self.values = path, None, {}
                return None
            if path != self.path or mtime != self.mtime:
                self.values = load_secrets_file(Path(path))
                self.path, self.mtime = path, mtime
            return self.values.get(name)


_FILE = _SecretsFile()


def load_secrets_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; '#' comments and blank lines skipped. The value is everything after the
    first '=', with matching outer quotes removed (for hand edits). Bad lines are ignored.
    A byte-order mark (Notepad) is skipped."""
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not _KEY.match(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if value:
            out[key] = value
            register_secret(value)
    return out


def secret(name: str) -> str | None:
    value = os.environ.get(name) or _FILE.get(name)
    if value:
        register_secret(value)
    return value or None


def register_secret(value: str) -> None:
    if value and len(value) >= 3:
        _SECRETS.add(value)


def mask(text: str | None) -> str | None:
    if not text:
        return text
    for s in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(s, "*****")
    text = _URL_SECRET.sub(lambda m: m.group(1) + "=*****", text)
    return re.sub(r"(?i)(password|passwd|community|token|api[_-]?key)=\S+", r"\1=*****", text)


def default_data_dir() -> Path:
    if os.environ.get("NETWORKGURU_DATA"):
        return Path(os.environ["NETWORKGURU_DATA"])
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "NetworkGuru"
    return Path("/data")


@dataclass
class Settings:
    inventory: Path
    rules: Path | None = None
    data_dir: Path = field(default_factory=default_data_dir)
    mode: str = "sim"                 # sim | live
    vm_url: str | None = None         # VictoriaMetrics; None = in-memory store
    host: str = "127.0.0.1"
    port: int = 8080
    history_days: int = 30
    fresh: bool = False               # sim: throw away stored history and rebuild it
    deploy: str = "dev"               # dev (this PC) | docker (site server, install.sh) | compose (git checkout)
    public_url: str = ""              # the dashboard address people open (report links)
    dev_admin_password: str | None = None   # run.py dev / compose: one-time password, never stored
