"""Feedback (0.9.5): anyone viewing the dashboard sends a request, a problem or an idea (the header's
Feedback button); signed-in admins track it on the Admin page; the morning report lists the new and
open ones.

Kept in the events database (events.py, table feedback) for good - prune() leaves it alone. The text
and the admin's note go through config.mask() before they are stored, so a pasted secret is never
kept. The sender's address is only used for the rate limit, in memory, never stored.
"""

from __future__ import annotations

import re
import time
from collections import deque

from .config import mask

KINDS = {"request": "Request", "problem": "Problem", "idea": "Idea"}
STATUSES = {"new": "New", "planned": "Planned", "doing": "In progress", "done": "Done", "wont": "Won't do"}
OPEN = ("new", "planned", "doing")
CLOSED = ("done", "wont")
MAX_TEXT, MAX_NAME, MAX_PAGE, MAX_NOTE = 2000, 60, 200, 1000
PER_HOUR = 20               # per address - behind Docker's NAT every viewer can share one
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _plain(s, limit: int, *, lines: bool = False) -> str:
    s = _CONTROL.sub("", str(s or "").replace("\r\n", "\n").replace("\t", " "))
    if not lines:
        s = " ".join(s.split())
    return s.strip()[:limit]


def clean(kind: str, text: str, name: str = "", page: str = "") -> tuple[str, str, str | None, str | None]:
    """(kind, text, name, page) as stored. ValueError with the reason for the sender."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    if len(str(text or "")) > MAX_TEXT:
        raise ValueError(f"Please keep it under {MAX_TEXT} characters.")
    body = _plain(text, MAX_TEXT, lines=True)
    if not body:
        raise ValueError("Write what you would like, or what is wrong.")
    page = _plain(page, MAX_PAGE)
    return kind, mask(body), mask(_plain(name, MAX_NAME)) or None, page if page.startswith("#/") else None


def clean_note(note) -> str:
    if len(str(note or "")) > MAX_NOTE:
        raise ValueError(f"The note must be under {MAX_NOTE} characters.")
    return mask(_plain(note, MAX_NOTE, lines=True))


class RateLimit:
    """At most `per_hour` sends per address in any hour."""

    def __init__(self, per_hour: int = PER_HOUR, clock=time.time):
        self.per_hour = per_hour
        self.clock = clock
        self.hits: dict[str, deque] = {}

    def allow(self, key: str) -> bool:
        now = self.clock()
        q = self.hits.setdefault(key, deque())
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= self.per_hour:
            return False
        q.append(now)
        if len(self.hits) > 1000:              # forget addresses that went quiet
            self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] <= 3600}
        return True


def summary_line(counts: dict) -> str:
    """The morning report's line, e.g. "2 new since yesterday, 5 open"."""
    return f"{counts['new_since']} new since yesterday, {counts['open']} open"
