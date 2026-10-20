"""Small JSON files in <data_dir>/state/ for the admin settings, update and report state.

They are kept apart from SQLite on purpose: on this PC SQLite lives in memory unless
VictoriaMetrics runs, but an approved update or "report sent today" must survive a restart.
Writes are atomic (temp file + rename).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("networkguru")


class StateFile:
    def __init__(self, path: Path, defaults: dict[str, Any] | None = None):
        self.path = Path(path)
        self.defaults = dict(defaults or {})
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {**self.defaults, **self._read()}

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("state file %s unreadable (%s) - starting from defaults", self.path.name, e)
            return {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def update(self, **values: Any) -> None:
        with self.lock:
            self.data.update(values)
            self._write()

    def replace(self, data: dict[str, Any]) -> None:
        with self.lock:
            self.data = {**self.defaults, **data}
            self._write()

    def _write(self) -> None:
        write_json_atomic(self.path, self.data)


def write_json_atomic(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise

