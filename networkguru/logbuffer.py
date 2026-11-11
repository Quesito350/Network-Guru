"""The last log lines in memory, for the problem report (Admin: Report a problem). The container
log keeps everything; this is only so a report can carry the recent part without a shell.

0.10.0: the log is also written to data/logs/networkguru.log (2 MB x 3, rotating) so it survives a
restart or an update; Admin → Download the log gives the newest LOG_DOWNLOAD_MAX of it, masked.
"""

from __future__ import annotations

import collections
import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

KEEP = 2000
LOG_FILE = "networkguru.log"
LOG_FILE_MAX = 2_000_000
LOG_FILE_KEEP = 3
LOG_DOWNLOAD_MAX = 4_000_000
_FORMAT = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")


class RingHandler(logging.Handler):
    def __init__(self, keep: int = KEEP):
        super().__init__(logging.INFO)
        self.lines: collections.deque[str] = collections.deque(maxlen=keep)
        self.lock2 = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = _FORMAT.format(record)
        except Exception:  # noqa: BLE001 - a bad log call must never break the app
            text = f"{record.levelname} {record.name}: (unformattable message)"
        with self.lock2:
            self.lines.append(text)

    def text(self) -> str:
        with self.lock2:
            return "\n".join(self.lines)

    def entries(self) -> list[str]:
        """One string per log record (a traceback stays with its line)."""
        with self.lock2:
            return list(self.lines)


def install() -> RingHandler:
    """Attach the buffer to the root logger once; later calls return the same one."""
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, RingHandler):
            return h
    h = RingHandler()
    root.addHandler(h)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return h


class _NgFile(RotatingFileHandler):
    """Network Guru's own log file (one per process; another data folder replaces it)."""


def install_file(log_dir: Path) -> _NgFile | None:
    """Also write the log to log_dir/networkguru.log. None if the folder can't be written."""
    root = logging.getLogger()
    target = (Path(log_dir) / LOG_FILE).resolve()
    for h in list(root.handlers):
        if isinstance(h, _NgFile):
            if Path(h.baseFilename).resolve() == target:
                return h
            root.removeHandler(h)
            h.close()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        h = _NgFile(target, maxBytes=LOG_FILE_MAX, backupCount=LOG_FILE_KEEP, encoding="utf-8", delay=True)
    except OSError:
        return None
    h.setLevel(logging.INFO)
    h.setFormatter(_FORMAT)
    root.addHandler(h)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return h


def file_text(log_dir: Path, limit: int = LOG_DOWNLOAD_MAX) -> str:
    """The newest `limit` bytes of the log file and the one before it (older first)."""
    for h in logging.getLogger().handlers:
        if isinstance(h, _NgFile):
            h.flush()
    base = Path(log_dir) / LOG_FILE
    parts: list[bytes] = []
    for p in (base.with_name(LOG_FILE + ".1"), base):
        try:
            parts.append(p.read_bytes())
        except OSError:
            pass
    data = b"".join(parts)
    if len(data) > limit:
        data = data[-limit:]
        cut = data.find(b"\n")
        data = data[cut + 1:] if cut >= 0 else data      # start on a whole line
    return data.decode("utf-8", "replace")
