"""FastAPI app: /api routers + static frontend (prod) + lifespan.

Lifespan: init DB → one-time FPL fetch (so the UI has data immediately) →
start scheduler → kick off the full news refresh as a background task (M3
revised: everything is fresh by the time the user finishes reading the
Dashboard) → shutdown.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config as cfgmod
from .api import lineups, meta, news, players, suggestions
from .db import init_db
from .fetchers import fpl as fpl_fetcher
from .httpclient import http
from .scheduler import create_scheduler

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("fpl.main")

DIST = cfgmod.ROOT / "frontend" / "dist"


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
    app = FastAPI(title="FPL Team Optimizer", version="1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(meta.router, prefix="/api")
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