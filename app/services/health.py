from __future__ import annotations

import asyncio
import html as html_module
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

import redis.asyncio as redis
from aiogram import Bot
from sqlalchemy import func, select, text

from app.config import settings
from app.database.database import async_session
from app.database.models import NewsCluster, Source


class HealthStatus:
    OK = "ok"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


SCHEDULER_MAX_AGE_MINUTES = 60
GENERATING_STUCK_AFTER_MINUTES = 30
DATABASE_HEALTH_TIMEOUT_SECONDS = 4.0
REDIS_HEALTH_TIMEOUT_SECONDS = 3.0
TELEGRAM_HEALTH_TIMEOUT_SECONDS = 3.0
HEALTH_FILE = Path("/tmp/health")

_QUEUE_STATUSES = ("new", "waiting", "generating", "pending_review")


@dataclass
class ComponentCheck:
    name: str
    status: str
    message: str = ""


@dataclass
class HealthCounters:
    active_sources: int = 0
    source_errors: int = 0
    new_clusters: int = 0
    waiting_clusters: int = 0
    generating_clusters: int = 0
    pending_review_clusters: int = 0
    stuck_generating_clusters: int = 0

    @property
    def queue_total(self) -> int:
        return (
            self.new_clusters
            + self.waiting_clusters
            + self.generating_clusters
            + self.pending_review_clusters
        )


@dataclass
class HealthReport:
    status: str
    generated_at: datetime
    components: list[ComponentCheck] = field(default_factory=list)
    counters: HealthCounters = field(default_factory=HealthCounters)
    last_cycle_at: Optional[datetime] = None
    last_cycle_errors: int = 0


DatabaseProbe = Callable[[datetime], Awaitable[tuple[ComponentCheck, HealthCounters]]]
RedisProbe = Callable[[], Awaitable[ComponentCheck]]
TelegramProbe = Callable[[], Awaitable[ComponentCheck]]


def read_scheduler_health_file(path: Path = HEALTH_FILE) -> Optional[datetime]:
    """Read the scheduler timestamp written for Docker health checks."""
    if not path.exists():
        return None

    try:
        ts = datetime.fromisoformat(path.read_text().strip())
    except Exception:
        return None

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def is_scheduler_recent(
    last_cycle_at: Optional[datetime],
    *,
    now: datetime,
    max_age_minutes: Optional[int] = None,
) -> bool:
    if max_age_minutes is None:
        # Scale with the configured interval: a 2h PARSING_INTERVAL_MINUTES
        # must not read as a dead scheduler between perfectly normal cycles.
        max_age_minutes = max(
            SCHEDULER_MAX_AGE_MINUTES, settings.parsing_interval_minutes * 2
        )
    if last_cycle_at is None:
        return False

    ts = last_cycle_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return now - ts <= timedelta(minutes=max_age_minutes)


def scheduler_check(
    last_cycle_at: Optional[datetime],
    last_cycle_errors: int,
    *,
    now: datetime,
) -> ComponentCheck:
    if not settings.scheduler_enabled:
        return ComponentCheck(
            name="scheduler",
            status=HealthStatus.OK,
            message="disabled",
        )

    if last_cycle_at is None:
        return ComponentCheck(
            name="scheduler",
            status=HealthStatus.UNHEALTHY,
            message="parsing cycle has not completed yet",
        )

    ts = last_cycle_at if last_cycle_at.tzinfo else last_cycle_at.replace(tzinfo=timezone.utc)
    age_minutes = max(0, int((now - ts).total_seconds() / 60))
    if not is_scheduler_recent(ts, now=now):
        return ComponentCheck(
            name="scheduler",
            status=HealthStatus.UNHEALTHY,
            message=f"last cycle {age_minutes} min ago",
        )

    if last_cycle_errors > 0:
        return ComponentCheck(
            name="scheduler",
            status=HealthStatus.DEGRADED,
            message=f"last cycle had {last_cycle_errors} error(s)",
        )

    return ComponentCheck(
        name="scheduler",
        status=HealthStatus.OK,
        message=f"last cycle {age_minutes} min ago",
    )


def source_check(counters: HealthCounters) -> ComponentCheck:
    if counters.source_errors > 0:
        return ComponentCheck(
            name="sources",
            status=HealthStatus.DEGRADED,
            message=f"{counters.source_errors} active source(s) have errors",
        )
    return ComponentCheck(
        name="sources",
        status=HealthStatus.OK,
        message=f"{counters.active_sources} active source(s)",
    )


def queue_check(counters: HealthCounters) -> ComponentCheck:
    if counters.stuck_generating_clusters > 0:
        return ComponentCheck(
            name="queue",
            status=HealthStatus.DEGRADED,
            message=f"{counters.stuck_generating_clusters} generating cluster(s) look stuck",
        )
    return ComponentCheck(
        name="queue",
        status=HealthStatus.OK,
        message=f"{counters.queue_total} active cluster(s)",
    )


def summarize_health_status(components: list[ComponentCheck]) -> str:
    statuses = {component.status for component in components}
    if HealthStatus.UNHEALTHY in statuses:
        return HealthStatus.UNHEALTHY
    if HealthStatus.DEGRADED in statuses:
        return HealthStatus.DEGRADED
    return HealthStatus.OK


async def collect_database_health(now: datetime) -> tuple[ComponentCheck, HealthCounters]:
    counters = HealthCounters()
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))

            counters.active_sources = await _count_scalar(
                session.scalar(
                    select(func.count()).select_from(Source).where(
                        Source.active == True,  # noqa: E712
                    )
                )
            )
            counters.source_errors = await _count_scalar(
                session.scalar(
                    select(func.count()).select_from(Source).where(
                        Source.active == True,  # noqa: E712
                        Source.last_error.isnot(None),
                    )
                )
            )

            rows = (
                await session.execute(
                    select(NewsCluster.status, func.count())
                    .where(NewsCluster.status.in_(_QUEUE_STATUSES))
                    .group_by(NewsCluster.status)
                )
            ).all()
            queue_counts = {status: int(count or 0) for status, count in rows}
            counters.new_clusters = queue_counts.get("new", 0)
            counters.waiting_clusters = queue_counts.get("waiting", 0)
            counters.generating_clusters = queue_counts.get("generating", 0)
            counters.pending_review_clusters = queue_counts.get("pending_review", 0)

            stuck_cutoff = now - timedelta(minutes=GENERATING_STUCK_AFTER_MINUTES)
            counters.stuck_generating_clusters = await _count_scalar(
                session.scalar(
                    select(func.count()).select_from(NewsCluster).where(
                        NewsCluster.status == "generating",
                        NewsCluster.updated_at < stuck_cutoff,
                    )
                )
            )
    except Exception as e:
        return (
            ComponentCheck(
                name="database",
                status=HealthStatus.UNHEALTHY,
                message=str(e)[:200],
            ),
            counters,
        )

    return (
        ComponentCheck(
            name="database",
            status=HealthStatus.OK,
            message="PostgreSQL reachable",
        ),
        counters,
    )


async def _count_scalar(value: Awaitable[object]) -> int:
    result = await value
    return int(result or 0)


async def collect_redis_health(
    *,
    redis_url: str = settings.redis_url,
    redis_factory: Optional[Callable[..., object]] = None,
) -> ComponentCheck:
    factory = redis_factory or redis.Redis.from_url
    try:
        client = factory(
            redis_url,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
    except Exception as e:
        return ComponentCheck(
            name="redis",
            status=HealthStatus.UNHEALTHY,
            message=str(e)[:200],
        )

    try:
        await client.ping()
    except Exception as e:
        return ComponentCheck(
            name="redis",
            status=HealthStatus.UNHEALTHY,
            message=str(e)[:200],
        )
    finally:
        close = getattr(client, "aclose", None) or getattr(client, "close", None)
        if close:
            result = close()
            if hasattr(result, "__await__"):
                await result

    return ComponentCheck(
        name="redis",
        status=HealthStatus.OK,
        message="Redis reachable",
    )


async def collect_telegram_health(
    *,
    bot_token: str = settings.bot_token,
    bot_factory: Optional[Callable[..., object]] = None,
    timeout_seconds: float = TELEGRAM_HEALTH_TIMEOUT_SECONDS,
) -> ComponentCheck:
    factory = bot_factory or Bot
    bot = None
    try:
        bot = factory(token=bot_token)
        me = await asyncio.wait_for(bot.get_me(), timeout=timeout_seconds)
        username = getattr(me, "username", None)
    except asyncio.TimeoutError:
        return ComponentCheck(
            name="telegram",
            status=HealthStatus.DEGRADED,
            message="Bot API check timed out",
        )
    except Exception as e:
        return ComponentCheck(
            name="telegram",
            status=HealthStatus.DEGRADED,
            message=str(e)[:200],
        )
    finally:
        if bot is not None:
            session = getattr(bot, "session", None)
            close = getattr(session, "close", None)
            if close:
                result = close()
                if hasattr(result, "__await__"):
                    await result

    if username:
        message = f"Bot API reachable (@{username})"
    else:
        message = "Bot API reachable"

    return ComponentCheck(
        name="telegram",
        status=HealthStatus.OK,
        message=message,
    )


async def collect_health_report(
    *,
    now: Optional[datetime] = None,
    last_cycle_at: Optional[datetime] = None,
    last_cycle_errors: int = 0,
    database_probe: Optional[DatabaseProbe] = None,
    redis_probe: Optional[RedisProbe] = None,
    telegram_probe: Optional[TelegramProbe] = None,
    database_timeout_seconds: float = DATABASE_HEALTH_TIMEOUT_SECONDS,
    redis_timeout_seconds: float = REDIS_HEALTH_TIMEOUT_SECONDS,
    telegram_timeout_seconds: float = TELEGRAM_HEALTH_TIMEOUT_SECONDS,
) -> HealthReport:
    current_time = now or datetime.now(timezone.utc)
    db_probe = database_probe or collect_database_health
    redis_probe_fn = redis_probe or collect_redis_health
    telegram_probe_fn = telegram_probe or collect_telegram_health

    db_result, redis_component, telegram_component = await asyncio.gather(
        _run_database_probe(
            db_probe,
            current_time,
            timeout_seconds=database_timeout_seconds,
        ),
        _run_component_probe(
            "redis",
            redis_probe_fn,
            timeout_seconds=redis_timeout_seconds,
            timeout_status=HealthStatus.UNHEALTHY,
        ),
        _run_component_probe(
            "telegram",
            telegram_probe_fn,
            timeout_seconds=telegram_timeout_seconds,
            timeout_status=HealthStatus.DEGRADED,
        ),
    )
    db_component, counters = db_result
    components = [
        scheduler_check(last_cycle_at, last_cycle_errors, now=current_time),
        db_component,
        redis_component,
        telegram_component,
    ]

    if db_component.status != HealthStatus.UNHEALTHY:
        components.append(source_check(counters))
        components.append(queue_check(counters))

    return HealthReport(
        status=summarize_health_status(components),
        generated_at=current_time,
        components=components,
        counters=counters,
        last_cycle_at=last_cycle_at,
        last_cycle_errors=last_cycle_errors,
    )


async def _run_database_probe(
    probe: DatabaseProbe,
    now: datetime,
    *,
    timeout_seconds: float,
) -> tuple[ComponentCheck, HealthCounters]:
    try:
        return await asyncio.wait_for(probe(now), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return (
            ComponentCheck(
                name="database",
                status=HealthStatus.UNHEALTHY,
                message="health check timed out",
            ),
            HealthCounters(),
        )
    except Exception as e:
        return (
            ComponentCheck(
                name="database",
                status=HealthStatus.UNHEALTHY,
                message=str(e)[:200],
            ),
            HealthCounters(),
        )


async def _run_component_probe(
    name: str,
    probe: Callable[[], Awaitable[ComponentCheck]],
    *,
    timeout_seconds: float,
    timeout_status: str,
) -> ComponentCheck:
    try:
        return await asyncio.wait_for(probe(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return ComponentCheck(
            name=name,
            status=timeout_status,
            message="health check timed out",
        )
    except Exception as e:
        return ComponentCheck(
            name=name,
            status=timeout_status,
            message=str(e)[:200],
        )


def format_health_report(report: HealthReport) -> str:
    status_label = {
        HealthStatus.OK: "OK",
        HealthStatus.DEGRADED: "DEGRADED",
        HealthStatus.UNHEALTHY: "UNHEALTHY",
    }.get(report.status, report.status.upper())

    lines = [
        "<b>Статус системы</b>",
        f"Проверено: <code>{_format_health_timestamp(report.generated_at)}</code>",
        f"Последний цикл: <code>{_format_health_timestamp(report.last_cycle_at)}</code>",
        "",
        f"Общий статус: <b>{html_module.escape(status_label)}</b>",
        "",
    ]

    for component in report.components:
        label = html_module.escape(_component_label(component.name))
        icon = _status_icon(component.status)
        message = html_module.escape(component.message or "—")
        lines.append(f"{icon} <b>{label}:</b> {message}")

    counters = report.counters
    lines.extend([
        "",
        "<b>Очередь</b>",
        f"New: <b>{counters.new_clusters}</b>",
        f"Waiting: <b>{counters.waiting_clusters}</b>",
        f"Generating: <b>{counters.generating_clusters}</b>",
        f"Pending review: <b>{counters.pending_review_clusters}</b>",
        f"Застряли в generating: <b>{counters.stuck_generating_clusters}</b>",
        "",
        f"Активных источников: <b>{counters.active_sources}</b>",
        f"Источников с ошибками: <b>{counters.source_errors}</b>",
        f"Ошибок в последнем цикле: <b>{report.last_cycle_errors}</b>",
    ])

    return "\n".join(lines)


def _component_label(name: str) -> str:
    return {
        "scheduler": "Scheduler",
        "database": "PostgreSQL",
        "redis": "Redis",
        "telegram": "Telegram",
        "sources": "Источники",
        "queue": "Очередь",
    }.get(name, name)


def _format_health_timestamp(value: Optional[datetime]) -> str:
    if value is None:
        return "—"

    ts = value
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts.strftime("%Y-%m-%d %H:%M:%S UTC")


def _status_icon(status: str) -> str:
    return {
        HealthStatus.OK: "✅",
        HealthStatus.DEGRADED: "⚠️",
        HealthStatus.UNHEALTHY: "❌",
    }.get(status, "•")
