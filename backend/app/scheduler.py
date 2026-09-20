"""APScheduler wiring.

M2: FPL bootstrap + news fetchers + signal extraction pass.
M3 (T3.8): adds the live-window conductor gating espn_live (30s) + fpl_live (60s).
All jobs: coalesce=True, max_instances=1, misfire_grace_time=30.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import load_settings

log = logging.getLogger("fpl.scheduler")

_JOB_KW = dict(coalesce=True, max_instances=1, misfire_grace_time=30)


async def _job_fpl() -> None:
    from .fetchers import fpl as fpl_fetcher

    try:
        await fpl_fetcher.refresh_all_fpl()
    except Exception:
        log.exception("scheduled fpl refresh failed")


async def _job_bbc() -> None:
    from .fetchers.bbc import refresh_bbc

    try:
        await refresh_bbc()
    except Exception:
        log.exception("scheduled bbc refresh failed")


async def _job_espn() -> None:
    from .fetchers.espn import refresh_espn

    try:
        await refresh_espn()
    except Exception:
        log.exception("scheduled espn refresh failed")


async def _job_reddit() -> None:
    from .fetchers.reddit import refresh_reddit

    try:
        await refresh_reddit(load_settings().config)
    except Exception:
        log.exception("scheduled reddit refresh failed")


async def _job_youtube() -> None:
    from .fetchers.youtube import refresh_youtube

    try:
        await refresh_youtube(load_settings().config)
    except Exception:
        log.exception("scheduled youtube refresh failed")


async def _job_extract() -> None:
    from .signals.pipeline import process_pending_items

    try:
        await process_pending_items(limit=50)
    except Exception:
        log.exception("scheduled extraction pass failed")


def create_scheduler(app) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")
    cfg = load_settings().config

    sched.add_job(_job_fpl, "interval",
                  minutes=cfg.sources.fpl.bootstrap_interval_min, id="fpl_bootstrap", **_JOB_KW)
    if cfg.sources.bbc.enabled:
        sched.add_job(_job_bbc, "interval", minutes=cfg.sources.bbc.interval_min,
                      id="bbc_news", **_JOB_KW)
    if cfg.sources.espn.enabled:
        sched.add_job(_job_espn, "interval", minutes=cfg.sources.espn.news_interval_min,
                      id="espn_news", **_JOB_KW)
    if cfg.sources.reddit.enabled:
        sched.add_job(_job_reddit, "interval", minutes=cfg.sources.reddit.interval_min,
                      id="reddit_news", **_JOB_KW)
    if cfg.sources.youtube.enabled:
        sched.add_job(_job_youtube, "interval", minutes=cfg.sources.youtube.interval_min,
                      id="youtube", **_JOB_KW)
    # extraction pass: drains the raw_items queue (LLM when ready, else rules)
    sched.add_job(_job_extract, "interval", minutes=10, id="extract", **_JOB_KW)
    return sched