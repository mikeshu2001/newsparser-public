from __future__ import annotations

from datetime import datetime, timezone

from scripts import create_local_test_draft


def test_smoke_external_id_is_stable_for_timestamp() -> None:
    now = datetime(2026, 6, 28, 20, 0, 0, tzinfo=timezone.utc)

    assert create_local_test_draft._smoke_external_id(now) == "local-smoke-1782676800"
