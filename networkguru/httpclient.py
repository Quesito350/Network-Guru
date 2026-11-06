"""Every HTTP client the server makes (0.9.6).

httpx builds a new TLS context for each AsyncClient - loading the certificate bundle, about 200 ms of
work that blocks the whole event loop while it runs. The readers make a client per poll (Mobius every
few seconds), and those stalls went into every ping and port time measured meanwhile: false latency
alarms. Here the TLS context is built once and shared; a client then costs a few milliseconds.
"""

from __future__ import annotations

import ssl

import certifi
import httpx

_SSL: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    global _SSL
    if _SSL is None:
        _SSL = ssl.create_default_context(cafile=certifi.where())
    return _SSL


def client(**kw) -> httpx.AsyncClient:
    """httpx.AsyncClient(**kw) with the shared TLS context (pass `verify` only to override it)."""
    kw.setdefault("verify", ssl_context())
    return httpx.AsyncClient(**kw)
