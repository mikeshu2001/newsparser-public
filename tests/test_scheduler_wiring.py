from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services import scheduler
from app.services.parsing_cycle import ParsingCycleResult


class _FakeScheduler:
    def __init__(self, *, running: bool = False):
        self.running = running
        self.jobs: list[dict[str, object]] = []
        self.started = False
        self.shutdown_calls: list[dict[str, object]] = []

    def add_job(self, func: object, trigger: str, **kwargs: object) -> None:
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})

    def start(self) -> None:
        self.started = True

    def shutdown(self, **kwargs: object) -> None:
        self.shutdown_calls.append(kwargs)


def test_start_scheduler_registers_parsing_and_cleanup_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeScheduler()
    monkeypatch.setattr(scheduler, "scheduler", fake)

    scheduler.start_scheduler()

    assert fake.started is True
    assert len(fake.jobs) == 2
    parsing_job, cleanup_job = fake.jobs
    assert parsing_job["func"] is scheduler.run_parsing_cycle
    assert parsing_job["trigger"] == "interval"
    assert parsing_job["minutes"] == scheduler.settings.parsing_interval_minutes
    assert parsing_job["id"] == "parsing_cycle"
    assert parsing_job["replace_existing"] is True
    assert isinstance(parsing_job["next_run_time"], datetime)
    assert cleanup_job == {
        "func": scheduler.run_cleanup,
        "trigger": "cron",
        "hour": 4,
        "minute": 0,
        "id": "cleanup",
        "replace_existing": True,
    }


def test_stop_scheduler_only_shuts_down_when_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = _FakeScheduler(running=True)
    monkeypatch.setattr(scheduler, "scheduler", stopped)

    scheduler.stop_scheduler()

    assert stopped.shutdown_calls == [{"wait": False}]

    not_running = _FakeScheduler(running=False)
    monkeypatch.setattr(scheduler, "scheduler", not_running)

    scheduler.stop_scheduler()

    assert not_running.shutdown_calls == []


async def test_run_parsing_cycle_updates_health_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_at = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)

    async def execute_parsing_cycle(**kwargs: object) -> ParsingCycleResult:
        assert kwargs["max_article_age_hours"] == scheduler._MAX_ARTICLE_AGE_HOURS
        assert kwargs["max_cluster_age_hours"] == scheduler._MAX_CLUSTER_AGE_HOURS
        assert kwargs["max_concurrent_fetches"] == scheduler._MAX_CONCURRENT_FETCHES
        assert kwargs["generating_timeout_minutes"] == scheduler._GENERATING_TIMEOUT_MINUTES
        assert kwargs["health_file"] == scheduler._HEALTH_FILE
        assert kwargs["consecutive_error_threshold"] == scheduler._CONSECUTIVE_ERROR_THRESHOLD
        return ParsingCycleResult(
            fetched=1,
            passed_filter=1,
            saved=1,
            duplicates=0,
            clusters_affected=1,
            errors=2,
            completed_at=completed_at,
        )

    monkeypatch.setattr(
        scheduler.parsing_cycle_service,
        "execute_parsing_cycle",
        execute_parsing_cycle,
    )
    monkeypatch.setattr(scheduler, "last_cycle_at", None)
    monkeypatch.setattr(scheduler, "last_cycle_errors", 0)

    await scheduler.run_parsing_cycle()

    assert scheduler.last_cycle_at == completed_at
    assert scheduler.last_cycle_errors == 2
