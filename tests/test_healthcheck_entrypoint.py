from __future__ import annotations

from datetime import datetime, timezone

import pytest

import healthcheck
from app.services.health import HealthReport, HealthStatus


def _report(status: HealthStatus) -> HealthReport:
    return HealthReport(
        status=status,
        generated_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (HealthStatus.OK, 0),
        (HealthStatus.DEGRADED, 0),
        (HealthStatus.UNHEALTHY, 1),
    ],
)
async def test_healthcheck_main_maps_status_to_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    status: HealthStatus,
    expected: int,
) -> None:
    async def collect_health_report(*args: object, **kwargs: object) -> HealthReport:
        return _report(status)

    monkeypatch.setattr(healthcheck, "collect_health_report", collect_health_report)
    monkeypatch.setattr(healthcheck, "read_scheduler_health_file", lambda: None)

    assert await healthcheck.main() == expected


async def test_healthcheck_main_returns_unhealthy_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def collect_health_report(*args: object, **kwargs: object) -> HealthReport:
        raise RuntimeError("db down")

    monkeypatch.setattr(healthcheck, "collect_health_report", collect_health_report)

    assert await healthcheck.main() == 1
