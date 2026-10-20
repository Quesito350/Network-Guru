"""FastAPI app: API + WebSocket + the static dashboard pages."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .. import TOOL_NAME, __version__
from ..report import as_html, report_from_runtime
from ..runtime import Runtime
from . import admin, api, ws

STATIC = Path(__file__).with_name("static")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self' ws: wss:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
# The report preview is the e-mail's own HTML (inline styles only), shown in a sandboxed frame.
PREVIEW_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'; base-uri 'none'"


def create_app(settings) -> FastAPI:
    rt = Runtime(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await rt.start()
        try:
            yield
        finally:
            await rt.stop()

    app = FastAPI(title=TOOL_NAME, version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.rt = rt
    app.include_router(api.router)
    app.include_router(admin.router)
    app.add_api_websocket_route("/ws/live", ws.live_ws)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/report/preview.html", include_in_schema=False)
    async def report_preview_html():
        return HTMLResponse(f"<!doctype html><html><head><meta charset=\"utf-8\"><title>Morning report</title>"
                            f"</head><body style=\"margin:12px\">{as_html(report_from_runtime(rt))}</body></html>",
                            headers={"Content-Security-Policy": PREVIEW_CSP})

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        path = request.url.path
        if path.startswith("/api") or path == "/" or path.startswith("/report/"):
            resp.headers["Cache-Control"] = "no-store"
        elif path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"    # revalidate: never mix modules after an update
        return resp

    return app
