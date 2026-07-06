from __future__ import annotations

import pathlib
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from app.config import settings
from app.services.cleanup import cleanup_old_data
from app.services import parsing_cycle as parsing_cycle_service

scheduler = AsyncIOScheduler()

_CONSECUTIVE_ERROR_THRESHOLD = 5

# If a cluster stays in 'generating' longer than this, revert to 'waiting'
_GENERATING_TIMEOUT_MINUTES = 30

# Don't promote or generate clusters older than this (hours since first_seen_at)
_MAX_CLUSTER_AGE_HOURS = 24

# Skip articles published more than this many hours ago (prevents ingesting feed history)
_MAX_ARTICLE_AGE_HOURS = 36

# Max concurrent source fetches (avoid overwhelming network / rate limits)
_MAX_CONCURRENT_FETCHES = 10

# Timestamp of last successful parsing cycle (for /health)
last_cycle_at: Optional[datetime] = None
last_cycle_errors: int = 0

# File-based healthcheck (Docker HEALTHCHECK reads this)
_HEALTH_FILE = pathlib.Path("/tmp/health")


def start_scheduler() -> None:
    """Start the parsing scheduler. Call from dp.startup hook."""
    scheduler.add_job(
        run_parsing_cycle,
        "interval",
        minutes=settings.parsing_interval_minutes,
        id="parsing_cycle",
        replace_existing=True,
        next_run_time=datetime.now(),  # run immediately on startup
    )
    scheduler.add_job(
        run_cleanup,
        "cron",
        hour=4,
        minute=0,
        id="cleanup",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        f"Scheduler started: parsing every {settings.parsing_interval_minutes} min, "
        f"cleanup daily at 04:00"
    )


def stop_scheduler() -> None:
    """Stop the scheduler gracefully. Call from dp.shutdown hook."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


async def run_parsing_cycle() -> None:
    """Main parsing cycle: fetch -> filter -> save -> dedup -> score -> lifecycle -> generate -> notify."""
    global last_cycle_at, last_cycle_errors

    result = await parsing_cycle_service.execute_parsing_cycle(
        max_article_age_hours=_MAX_ARTICLE_AGE_HOURS,
        max_cluster_age_hours=_MAX_CLUSTER_AGE_HOURS,
        max_concurrent_fetches=_MAX_CONCURRENT_FETCHES,
        generating_timeout_minutes=_GENERATING_TIMEOUT_MINUTES,
        health_file=_HEALTH_FILE,
        consecutive_error_threshold=_CONSECUTIVE_ERROR_THRESHOLD,
    )
    last_cycle_at = result.completed_at
    last_cycle_errors = result.errors


async def run_cleanup() -> None:
    """Cleanup old data: raw_articles > 30 days, rejected > 7 days, approved > 90 days."""
    logger.info("Cleanup job started")

    deleted_raw, deleted_rejected, deleted_approved = await cleanup_old_data()

    logger.info(
        f"Cleanup done: raw_articles={deleted_raw}, "
        f"rejected_clusters={deleted_rejected}, "
        f"old_approved_clusters={deleted_approved}"
    )
