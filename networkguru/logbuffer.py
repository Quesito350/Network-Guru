"""The last log lines in memory, for the problem report (Admin: Report a problem). The container
log keeps everything; this is only so a report can carry the recent part without a shell."""

from __future__ import annotations

import collections
import logging
import threading

KEEP = 2000
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
