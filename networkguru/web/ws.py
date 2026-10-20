"""WebSocket /ws/live: a full snapshot on connect, then only what changed, once a second."""

from __future__ import annotations

import asyncio
import time

from fastapi import WebSocket, WebSocketDisconnect


async def live_ws(ws: WebSocket) -> None:
    await ws.accept()
    r = ws.app.state.rt
    snap = r.pipeline.snapshot()
    await ws.send_json({"type": "snapshot", "backfill": round(r.progress.pct, 1), **snap})
    last = snap["version"]
    last_sent = time.time()
    try:
        while True:
            await asyncio.sleep(1.0)
            if r.pipeline.version != last:
                d = r.pipeline.delta_since(last)
                last = d["version"]
                await ws.send_json({"type": "delta", "backfill": round(r.progress.pct, 1), **d})
                last_sent = time.time()
            elif time.time() - last_sent > 15:
                await ws.send_json({"type": "hb", "ts": time.time()})
                last_sent = time.time()
    except (WebSocketDisconnect, RuntimeError):
        return
