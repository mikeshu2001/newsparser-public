from __future__ import annotations

import asyncio
import html as html_module
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from loguru import logger
from sqlalchemy import select

from app.database.database import async_session
from app.database.models import BotUser, Source, Workspace
from app.parsers import get_parser
from app.parsers.base import ParserError
from app.services.cluster_pipeline import (
    check_new_clusters,
    check_waiting_clusters,
    expire_old_clusters,
    recover_stuck_clusters,
    score_and_transition,
)
from app.services.generation_pipeline import generate_and_notify
from app.services.ingestion import parse_source, update_source_error


@dataclass(frozen=True)
class ParsingCycleResult:
    fetched: int
    passed_filter: int
    saved: int
    duplicates: int
    clusters_affected: int
    errors: int
    completed_at: datetime


# Track consecutive parsing errors per source (in-memory, resets on restart)
consecutive_errors: dict[int, int] = {}


async def _run_stage(name: str, stage: Awaitable[object]) -> None:
    """A failed stage must not abort the cycle or skip the health-file write."""
    try:
        await stage
    except Exception as e:
        logger.error(f"Parsing cycle stage '{name}' failed: {e}")


async def execute_parsing_cycle(
    *,
    max_article_age_hours: int,
    max_cluster_age_hours: int,
    max_concurrent_fetches: int,
    generating_timeout_minutes: int,
    health_file: Path,
    consecutive_error_threshold: int = 5,
    session_factory: Callable[[], object] = async_session,
    parser_factory: Callable[[str], object] = get_parser,
) -> ParsingCycleResult:
    """Execute one full parsing cycle."""
    logger.info("Parsing cycle started")

    async with session_factory() as session:
        workspaces = {
            workspace.id: workspace
            for workspace in (
                await session.scalars(
                    select(Workspace).where(Workspace.active == True)  # noqa: E712
                )
            ).all()
        }
        sources = (
            await session.scalars(
                select(Source).where(
                    Source.active == True,  # noqa: E712
                    Source.workspace_id.in_(list(workspaces)),
                )
            )
        ).all()

    total_fetched = 0
    total_passed = 0
    total_saved = 0
    total_dupes = 0
    cycle_errors = 0
    affected_cluster_ids: set[int] = set()

    semaphore = asyncio.Semaphore(max_concurrent_fetches)

    async def _parse_one(source: Source) -> None:
        nonlocal total_fetched, total_passed, total_saved, total_dupes, cycle_errors
        async with semaphore:
            try:
                fetched, passed, saved, dupes, cluster_ids = await parse_source(
                    source,
                    max_article_age_hours=max_article_age_hours,
                    parser_factory=parser_factory,
                    workspace=workspaces.get(source.workspace_id),
                )
                total_fetched += fetched
                total_passed += passed
                total_saved += saved
                total_dupes += dupes
                affected_cluster_ids.update(cluster_ids)
                consecutive_errors.pop(source.id, None)
            except ParserError as e:
                error_text = str(e)
                logger.warning(f"Parser error for {source.name}: {error_text}")
                source.last_error = error_text
                await update_source_error(source.id, error_text)
                cycle_errors += 1
                await track_consecutive_error(
                    source,
                    threshold=consecutive_error_threshold,
                    session_factory=session_factory,
                )
            except Exception as e:
                error_text = str(e)
                logger.error(f"Unexpected error parsing {source.name}: {error_text}")
                source.last_error = error_text
                await update_source_error(source.id, error_text)
                cycle_errors += 1
                await track_consecutive_error(
                    source,
                    threshold=consecutive_error_threshold,
                    session_factory=session_factory,
                )

    await asyncio.gather(*[_parse_one(s) for s in sources])

    if affected_cluster_ids:
        await _run_stage(
            "score_and_transition", score_and_transition(affected_cluster_ids)
        )

    await _run_stage(
        "check_waiting_clusters",
        check_waiting_clusters(max_cluster_age_hours=max_cluster_age_hours),
    )
    await _run_stage(
        "check_new_clusters",
        check_new_clusters(max_cluster_age_hours=max_cluster_age_hours),
    )
    await _run_stage(
        "expire_old_clusters",
        expire_old_clusters(max_cluster_age_hours=max_cluster_age_hours),
    )
    await _run_stage(
        "recover_stuck_clusters",
        recover_stuck_clusters(generating_timeout_minutes=generating_timeout_minutes),
    )
    await _run_stage(
        "generate_and_notify",
        generate_and_notify(max_cluster_age_hours=max_cluster_age_hours),
    )

    completed_at = datetime.now(timezone.utc)
    health_file.write_text(completed_at.isoformat())

    logger.info(
        f"Parsing cycle done: fetched={total_fetched}, "
        f"passed_filter={total_passed}, saved={total_saved}, "
        f"duplicates={total_dupes}, clusters_affected={len(affected_cluster_ids)}"
    )

    return ParsingCycleResult(
        fetched=total_fetched,
        passed_filter=total_passed,
        saved=total_saved,
        duplicates=total_dupes,
        clusters_affected=len(affected_cluster_ids),
        errors=cycle_errors,
        completed_at=completed_at,
    )


async def track_consecutive_error(
    source: Source,
    *,
    threshold: int = 5,
    session_factory: Callable[[], object] = async_session,
) -> None:
    """Track consecutive errors and notify admins if threshold is reached."""
    count = consecutive_errors.get(source.id, 0) + 1
    consecutive_errors[source.id] = count

    if count == threshold:
        logger.warning(
            f"Source '{source.name}' failed {count} times in a row - notifying admins"
        )
        await notify_admins_error(
            source,
            count,
            session_factory=session_factory,
        )


async def notify_admins_error(
    source: Source,
    error_count: int,
    *,
    session_factory: Callable[[], object] = async_session,
) -> None:
    """Send alert to admins about repeated parsing failures."""
    from app.services.notifier import get_bot

    try:
        bot = get_bot()
    except RuntimeError:
        logger.warning("Bot not available for error notification")
        return

    async with session_factory() as session:
        admins = (
            await session.scalars(
                select(BotUser).where(
                    BotUser.role == "admin",
                    BotUser.is_active == True,  # noqa: E712
                )
            )
        ).all()

    text = format_source_error_alert(source, error_count)

    for admin in admins:
        try:
            await bot.send_message(chat_id=admin.id, text=text)
        except Exception as e:
            logger.error(f"Failed to notify admin {admin.id} about source error: {e}")


def format_source_error_alert(source: Source, error_count: int) -> str:
    source_name = html_module.escape(source.name)
    last_error = html_module.escape((source.last_error or "unknown")[:200])
    return (
        f"⚠️ <b>Ошибка парсинга</b>\n\n"
        f"Источник <b>{source_name}</b> не работает "
        f"{error_count} циклов подряд.\n"
        f"Последняя ошибка: <code>{last_error}</code>\n\n"
        f"Проверьте /sources и отключите при необходимости."
    )
