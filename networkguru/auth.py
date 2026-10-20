"""Admin sign-in: one admin password (hashed with scrypt in secrets.env), short-lived sessions.

The dashboard itself stays open to everyone on the site network. Only the admin actions
(approve / postpone / skip an update, upload an update, change report settings, send a test
report) need a signed-in admin.

Hash format: scrypt:<N>:<r>:<p>:<salt b64>:<hash b64>. deploy/ng_updater.py writes the same
format on the server (install.sh) - a test checks both agree.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets as _rand
import time
from dataclasses import dataclass, field

from .config import secret

HASH_ENV = "NETWORKGURU_ADMIN_PASSWORD_HASH"
N, R, P, DKLEN = 32768, 8, 1, 32
MAXMEM = 64 * 1024 * 1024
MIN_LEN = 10
IDLE_S = 8 * 3600
MAX_AGE_S = 24 * 3600
COOKIE = "ng_admin"


def hash_password(password: str, n: int = N) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=R, p=P, dklen=DKLEN, maxmem=MAXMEM)
    return f"scrypt:{n}:{R}:{P}:{base64.b64encode(salt).decode()}:{base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = (stored or "").strip().split(":")
        if algo != "scrypt":
            return False
        want = base64.b64decode(hash_b64)
        got = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64), n=int(n), r=int(r),
                             p=int(p), dklen=len(want), maxmem=MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def lockout_seconds(failures: int) -> float:
    """Wait before the next try: none for the first 2 mistakes, then 10 s, 60 s, 5 min."""
    return {0: 0, 1: 0, 2: 0, 3: 10, 4: 60}.get(failures, 300)


@dataclass
class Session:
    name: str
    created: float
    seen: float


@dataclass
class _Tries:
    failures: int = 0
    until: float = 0.0


@dataclass
class AdminAuth:
    dev_hash: str | None = None                 # run.py dev: hash of the one-time password
    clock: object = time.time
    sessions: dict[str, Session] = field(default_factory=dict)
    tries: dict[str, _Tries] = field(default_factory=dict)

    def __post_init__(self):
        self._sem: asyncio.Semaphore | None = None   # at most 2 scrypt checks at once (32 MiB each)

    def stored_hash(self) -> str | None:
        return secret(HASH_ENV) or self.dev_hash

    @property
    def configured(self) -> bool:
        return bool(self.stored_hash())

    def wait_for(self, client: str) -> float:
        t = self.tries.get(client)
        return max(0.0, t.until - self.clock()) if t else 0.0

    async def login(self, client: str, password: str, name: str) -> str | None:
        """A new session token, or None for a wrong password. Raise nothing: callers check wait_for()."""
        if self._sem is None:
            self._sem = asyncio.Semaphore(2)
        async with self._sem:
            ok = await asyncio.to_thread(verify_password, password or "", self.stored_hash())
        now = self.clock()
        t = self.tries.setdefault(client, _Tries())
        if not ok:
            t.failures += 1
            t.until = now + lockout_seconds(t.failures)
            return None
        self.tries.pop(client, None)
        token = _rand.token_urlsafe(32)
        self.sessions[token] = Session(name=(name or "admin").strip()[:40] or "admin", created=now, seen=now)
        return token

    def session(self, token: str | None) -> Session | None:
        if not token:
            return None
        s = self.sessions.get(token)
        now = self.clock()
        if s is None:
            return None
        if now - s.seen > IDLE_S or now - s.created > MAX_AGE_S or not self.configured:
            self.sessions.pop(token, None)
            return None
        s.seen = now
        return s

    def logout(self, token: str | None) -> None:
        if token:
            self.sessions.pop(token, None)
