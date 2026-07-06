from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.services import health as health_module
from app.services.health import (
    ComponentCheck,
    HealthCounters,
    HealthReport,
    HealthStatus,
    collect_health_report,
    collect_redis_health,
    collect_telegram_health,
    format_health_report,
    is_scheduler_recent,
    queue_check,
    scheduler_check,
    source_check,
    summarize_health_status,
)


@pytest.fixture(autouse=True)
def _enable_scheduler_for_health_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_module.settings, "scheduler_enabled", True)


def test_scheduler_recent_helper_handles_missing_stale_and_recent() -> None:
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)

    assert not is_scheduler_recent(None, now=now)
    assert not is_scheduler_recent(now - timedelta(minutes=61), now=now)
    assert is_scheduler_recent(now - timedelta(minutes=5), now=now)


def test_scheduler_check_reports_errors_as_degraded() -> None:
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)

    check = scheduler_check(now - timedelta(minutes=2), 3, now=now)

    assert check.name == "scheduler"
    assert check.status == HealthStatus.DEGRADED
    assert "3 error" in check.message


def test_scheduler_check_reports_disabled_as_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_module.settings, "scheduler_enabled", False)

    check = scheduler_check(None, 0, now=datetime.now(timezone.utc))

    assert check.name == "scheduler"
    assert check.status == HealthStatus.OK
    assert check.message == "disabled"


def test_scheduler_check_clamps_future_timestamp_age_display() -> None:
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)

    check = scheduler_check(now + timedelta(minutes=5), 0, now=now)

    assert check.status == HealthStatus.OK
    assert check.message == "last cycle 0 min ago"


def test_summarize_health_status_prefers_unhealthy_over_degraded() -> None:
    assert summarize_health_status([
        ComponentCheck("a", HealthStatus.OK),
        ComponentCheck("b", HealthStatus.DEGRADED),
    ]) == HealthStatus.DEGRADED
    assert summarize_health_status([
        ComponentCheck("a", HealthStatus.OK),
        ComponentCheck("b", HealthStatus.UNHEALTHY),
        ComponentCheck("c", HealthStatus.DEGRADED),
    ]) == HealthStatus.UNHEALTHY


def test_source_and_queue_checks_mark_operational_risks_degraded() -> None:
    counters = HealthCounters(
        active_sources=10,
        source_errors=2,
        new_clusters=1,
        waiting_clusters=1,
        generating_clusters=1,
        pending_review_clusters=1,
        stuck_generating_clusters=1,
    )

    assert source_check(counters).status == HealthStatus.DEGRADED
    assert queue_check(counters).status == HealthStatus.DEGRADED
    assert counters.queue_total == 4


async def test_collect_health_report_combines_injected_probes() -> None:
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
    counters = HealthCounters(active_sources=4, source_errors=1)

    async def db_probe(current_time: datetime) -> tuple[ComponentCheck, HealthCounters]:
        assert current_time == now
        return ComponentCheck("database", HealthStatus.OK, "db ok"), counters

    async def redis_probe() -> ComponentCheck:
        return ComponentCheck("redis", HealthStatus.OK, "redis ok")

    async def telegram_probe() -> ComponentCheck:
        return ComponentCheck("telegram", HealthStatus.OK, "telegram ok")

    report = await collect_health_report(
        now=now,
        last_cycle_at=now - timedelta(minutes=3),
        last_cycle_errors=0,
        database_probe=db_probe,
        redis_probe=redis_probe,
        telegram_probe=telegram_probe,
    )

    assert report.status == HealthStatus.DEGRADED
    assert [component.name for component in report.components] == [
        "scheduler",
        "database",
        "redis",
        "telegram",
        "sources",
        "queue",
    ]
    assert report.counters.source_errors == 1


async def test_collect_health_report_marks_db_failure_unhealthy() -> None:
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)

    async def db_probe(current_time: datetime) -> tuple[ComponentCheck, HealthCounters]:
        return (
            ComponentCheck("database", HealthStatus.UNHEALTHY, "db down"),
            HealthCounters(),
        )

    async def redis_probe() -> ComponentCheck:
        return ComponentCheck("redis", HealthStatus.OK, "redis ok")

    async def telegram_probe() -> ComponentCheck:
        return ComponentCheck("telegram", HealthStatus.OK, "telegram ok")

    report = await collect_health_report(
        now=now,
        last_cycle_at=now - timedelta(minutes=1),
        database_probe=db_probe,
        redis_probe=redis_probe,
        telegram_probe=telegram_probe,
    )

    assert report.status == HealthStatus.UNHEALTHY
    assert [component.name for component in report.components] == [
        "scheduler",
        "database",
        "redis",
        "telegram",
    ]


async def test_collect_health_report_marks_probe_timeouts() -> None:
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)

    async def slow_db_probe(
        current_time: datetime,
    ) -> tuple[ComponentCheck, HealthCounters]:
        await asyncio.sleep(1)
        return ComponentCheck("database", HealthStatus.OK), HealthCounters()

    async def slow_redis_probe() -> ComponentCheck:
        await asyncio.sleep(1)
        return ComponentCheck("redis", HealthStatus.OK)

    async def slow_telegram_probe() -> ComponentCheck:
        await asyncio.sleep(1)
        return ComponentCheck("telegram", HealthStatus.OK)

    report = await collect_health_report(
        now=now,
        last_cycle_at=now - timedelta(minutes=1),
        database_probe=slow_db_probe,
        redis_probe=slow_redis_probe,
        telegram_probe=slow_telegram_probe,
        database_timeout_seconds=0.01,
        redis_timeout_seconds=0.01,
        telegram_timeout_seconds=0.01,
    )

    components = {component.name: component for component in report.components}

    assert report.status == HealthStatus.UNHEALTHY
    assert components["database"].status == HealthStatus.UNHEALTHY
    assert components["redis"].status == HealthStatus.UNHEALTHY
    assert components["telegram"].status == HealthStatus.DEGRADED
    assert "timed out" in components["database"].message


class _FakeRedis:
    def __init__(self, *, should_fail: bool = False):
        self.should_fail = should_fail
        self.closed = False

    async def ping(self) -> bool:
        if self.should_fail:
            raise RuntimeError("redis down")
        return True

    async def aclose(self) -> None:
        self.closed = True


async def test_collect_redis_health_closes_client_on_success() -> None:
    client = _FakeRedis()

    def factory(*args: object, **kwargs: object) -> _FakeRedis:
        return client

    check = await collect_redis_health(
        redis_url="redis://test",
        redis_factory=factory,
    )

    assert check.status == HealthStatus.OK
    assert client.closed is True


async def test_collect_redis_health_closes_client_on_failure() -> None:
    client = _FakeRedis(should_fail=True)

    def factory(*args: object, **kwargs: object) -> _FakeRedis:
        return client

    check = await collect_redis_health(
        redis_url="redis://test",
        redis_factory=factory,
    )

    assert check.status == HealthStatus.UNHEALTHY
    assert "redis down" in check.message
    assert client.closed is True


async def test_collect_redis_health_handles_factory_failure() -> None:
    def factory(*args: object, **kwargs: object) -> _FakeRedis:
        raise RuntimeError("bad redis url")

    check = await collect_redis_health(
        redis_url="redis://test",
        redis_factory=factory,
    )

    assert check.status == HealthStatus.UNHEALTHY
    assert "bad redis url" in check.message


class _FakeBotSession:
    def __init__(self):
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeBot:
    def __init__(self, *, should_fail: bool = False, should_timeout: bool = False):
        self.should_fail = should_fail
        self.should_timeout = should_timeout
        self.session = _FakeBotSession()

    async def get_me(self):
        if self.should_timeout:
            await asyncio.sleep(1)
        if self.should_fail:
            raise RuntimeError("telegram down")

        class _Me:
            username = "test_bot"

        return _Me()


async def test_collect_telegram_health_closes_session_on_success() -> None:
    bot = _FakeBot()

    def factory(*args: object, **kwargs: object) -> _FakeBot:
        assert kwargs["token"] == "token"
        return bot

    check = await collect_telegram_health(
        bot_token="token",
        bot_factory=factory,
    )

    assert check.name == "telegram"
    assert check.status == HealthStatus.OK
    assert "@test_bot" in check.message
    assert bot.session.closed is True


async def test_collect_telegram_health_closes_session_on_failure() -> None:
    bot = _FakeBot(should_fail=True)

    def factory(*args: object, **kwargs: object) -> _FakeBot:
        return bot

    check = await collect_telegram_health(
        bot_token="token",
        bot_factory=factory,
    )

    assert check.name == "telegram"
    assert check.status == HealthStatus.DEGRADED
    assert "telegram down" in check.message
    assert bot.session.closed is True


async def test_collect_telegram_health_closes_session_on_timeout() -> None:
    bot = _FakeBot(should_timeout=True)

    def factory(*args: object, **kwargs: object) -> _FakeBot:
        return bot

    check = await collect_telegram_health(
        bot_token="token",
        bot_factory=factory,
        timeout_seconds=0.01,
    )

    assert check.name == "telegram"
    assert check.status == HealthStatus.DEGRADED
    assert "timed out" in check.message
    assert bot.session.closed is True


def test_format_health_report_escapes_dynamic_component_messages() -> None:
    report = HealthReport(
        status=HealthStatus.UNHEALTHY,
        generated_at=datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc),
        components=[
            ComponentCheck(
                "database",
                HealthStatus.UNHEALTHY,
                "<script>down</script>",
            )
        ],
        counters=HealthCounters(active_sources=2, source_errors=1),
        last_cycle_errors=5,
    )

    text = format_health_report(report)

    assert "&lt;script&gt;down&lt;/script&gt;" in text
    assert "<script>down</script>" not in text
    assert "Источников с ошибками" in text


def test_format_health_report_includes_utc_operational_timestamps() -> None:
    report = HealthReport(
        status=HealthStatus.DEGRADED,
        generated_at=datetime(2026, 6, 28, 12, 0, 5, tzinfo=timezone.utc),
        components=[ComponentCheck("scheduler", HealthStatus.DEGRADED, "late")],
        counters=HealthCounters(
            new_clusters=4,
            waiting_clusters=1,
            generating_clusters=2,
            pending_review_clusters=3,
        ),
        last_cycle_at=datetime(
            2026,
            6,
            28,
            15,
            30,
            tzinfo=timezone(timedelta(hours=3)),
        ),
        last_cycle_errors=2,
    )

    text = format_health_report(report)

    assert "Проверено: <code>2026-06-28 12:00:05 UTC</code>" in text
    assert "Последний цикл: <code>2026-06-28 12:30:00 UTC</code>" in text
    assert "Общий статус: <b>DEGRADED</b>" in text
    assert "New: <b>4</b>" in text
    assert "Waiting: <b>1</b>" in text
    assert "Generating: <b>2</b>" in text
    assert "Pending review: <b>3</b>" in text
    assert "Ошибок в последнем цикле: <b>2</b>" in text


def test_format_health_report_handles_missing_last_cycle_timestamp() -> None:
    report = HealthReport(
        status=HealthStatus.UNHEALTHY,
        generated_at=datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc),
        components=[],
        last_cycle_at=None,
    )

    text = format_health_report(report)

    assert "Последний цикл: <code>—</code>" in text


def test_format_health_report_escapes_unknown_component_labels() -> None:
    report = HealthReport(
        status=HealthStatus.OK,
        generated_at=datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc),
        components=[
            ComponentCheck(
                "<custom>",
                HealthStatus.OK,
                "ok",
            )
        ],
    )

    text = format_health_report(report)

    assert "&lt;custom&gt;" in text
    assert "<custom>" not in text
