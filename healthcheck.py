"""Docker HEALTHCHECK script."""

from __future__ import annotations

import asyncio
import sys

from app.services.health import (
    HealthStatus,
    collect_health_report,
    read_scheduler_health_file,
)


async def main() -> int:
    try:
        report = await collect_health_report(
            last_cycle_at=read_scheduler_health_file(),
            last_cycle_errors=0,
        )
    except Exception:
        return 1

    if report.status == HealthStatus.UNHEALTHY:
        return 1
    return 0


def cli_main() -> None:
    sys.exit(asyncio.run(main()))


if __name__ == "__main__":
    cli_main()
