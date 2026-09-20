"""APScheduler wiring.

M1: stub — startup fetch + on-demand refresh only (POST /api/refresh/{source}).
M3 (T3.8): full job table with live-window conductor.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger("fpl.scheduler")


def create_scheduler(app) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")
    # M1: no scheduled jobs yet. M3 registers:
    #   fpl_bootstrap 15m, news fetchers (bbc 30m / espn 30m / reddit 30m / youtube 60m),
    #   live conductor 60s gating espn_live (30s) + fpl_live (60s).
    # All jobs: coalesce=True, max_instances=1, misfire_grace_time=30.
    return sched