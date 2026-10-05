"""FastAPI app: /api routers + static frontend (prod) + lifespan.

Lifespan: init DB → one-time FPL fetch (so the UI has data immediately) →
start scheduler → kick off the full news refresh as a background task (M3
revised: everything is fresh by the time the user finishes reading the
Dashboard) → shutdown.

Security (v1.0): the launchers bind 127.0.0.1 only. On top of that,
``LocalOnlyGuard`` refuses requests whose Host is not a loopback name
(DNS-rebinding) and state-changing requests whose Origin is another site
(CSRF from a page open in your browser). Extra host names can be allowed with
FPL_ALLOWED_HOSTS=name1,name2 if you deliberately serve the app elsewhere.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config as cfgmod
from .api import entry, lineups, meta, news, players, suggestions
from .db import init_db
from .fetchers import fpl as fpl_fetcher
from .httpclient import http
from .scheduler import create_scheduler

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("fpl.main")

DIST = cfgmod.ROOT / "frontend" / "dist"
VERSION = "1.0.0"

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _allowed_hosts() -> set[str]:
    extra = {h.strip().lower() for h in os.getenv("FPL_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return _LOOPBACK | extra


class LocalOnlyGuard:
    """ASGI middleware: loopback Host only; same-machine Origin for writes."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        allowed = _allowed_hosts()
        host = urlsplit("//" + headers.get("host", "")).hostname or ""
        if host.lower() not in allowed:
            return await self._deny(send, "host not allowed: this app only answers on localhost")
        origin = headers.get("origin")
        if scope["method"] in _UNSAFE and origin and origin != "null":
            ohost = (urlsplit(origin).hostname or "").lower()
            if ohost not in allowed:
                return await self._deny(send, "cross-site request refused")
        elif scope["method"] in _UNSAFE and origin == "null":
            return await self._deny(send, "cross-site request refused")
        return await self.app(scope, receive, send)

    @staticmethod
    async def _deny(send, message: str):
        body = ('{"detail": "%s"}' % message).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        await fpl_fetcher.refresh_all_fpl()
        log.info("startup FPL fetch complete")
    except Exception as e:
        log.warning("startup FPL fetch failed (app continues on last-good data): %s", e)
    sched = create_scheduler(app)
    sched.start()
    app.state.scheduler = sched
    # Full news refresh in the background: app is already serving FPL data;
    # BBC/Reddit/YouTube + signal extraction land shortly after (UI polls
    # /api/meta/startup-refresh and notifies when it finishes).
    from .startup import run_full_refresh

    app.state.startup_refresh = asyncio.create_task(run_full_refresh())
    yield
    try:
        sched.shutdown(wait=False)
    except Exception:
        pass
    task = getattr(app.state, "startup_refresh", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    await http.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="FPL Team Optimizer", version=VERSION, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(LocalOnlyGuard)
    app.include_router(meta.router, prefix="/api")
    app.include_router(entry.router, prefix="/api")
    app.include_router(players.router, prefix="/api")
    app.include_router(lineups.router, prefix="/api")
    app.include_router(suggestions.router, prefix="/api")
    app.include_router(news.router, prefix="/api")

    if DIST.exists():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path.startswith("api/"):
                return JSONResponse({"detail": "not found"}, status_code=404)
            file = DIST / path
            if path and file.exists() and file.is_file():
                return FileResponse(file)
            return FileResponse(DIST / "index.html")
    return app


app = create_app()