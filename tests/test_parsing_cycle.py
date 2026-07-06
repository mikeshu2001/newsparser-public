from __future__ import annotations

from datetime import datetime

from app.database.models import Source
from app.parsers.base import ParserError
from app.services import parsing_cycle


class _ScalarResult:
    def __init__(self, items: list[object]):
        self._items = items

    def all(self) -> list[object]:
        return self._items


class _FakeSession:
    def __init__(self, *result_sets: list[object]):
        self._result_sets = list(result_sets)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def scalars(self, statement: object) -> _ScalarResult:
        return _ScalarResult(self._result_sets.pop(0))


def _source() -> Source:
    return Source(
        id=1,
        workspace_id=1,
        name="Source <One>",
        url="https://example.com/feed.xml",
        type="rss",
        weight=5,
        active=True,
    )


def _workspace() -> object:
    from types import SimpleNamespace

    return SimpleNamespace(id=1, active=True)


def _cycle_session(sources: list[object]) -> _FakeSession:
    """Cycle opens one session: workspaces query, then sources query."""
    return _FakeSession([_workspace()], sources)


async def test_execute_parsing_cycle_aggregates_success_and_writes_health_file(
    monkeypatch,
    tmp_path,
) -> None:
    source = _source()
    parsing_cycle.consecutive_errors[source.id] = 2
    calls: list[tuple[str, object]] = []

    async def fake_parse_source(
        parsed_source: Source,
        *,
        max_article_age_hours: int,
        parser_factory: object,
        workspace: object = None,
    ) -> tuple[int, int, int, int, set[int]]:
        assert parsed_source is source
        assert max_article_age_hours == 36
        return 2, 1, 1, 1, {7}

    async def fake_score(cluster_ids: set[int]) -> None:
        calls.append(("score", cluster_ids))

    async def fake_waiting(*, max_cluster_age_hours: int) -> None:
        calls.append(("waiting", max_cluster_age_hours))

    async def fake_new(*, max_cluster_age_hours: int) -> None:
        calls.append(("new", max_cluster_age_hours))

    async def fake_expire(*, max_cluster_age_hours: int) -> None:
        calls.append(("expire", max_cluster_age_hours))

    async def fake_recover(*, generating_timeout_minutes: int) -> None:
        calls.append(("recover", generating_timeout_minutes))

    async def fake_generate(*, max_cluster_age_hours: int) -> None:
        calls.append(("generate", max_cluster_age_hours))

    monkeypatch.setattr(parsing_cycle, "parse_source", fake_parse_source)
    monkeypatch.setattr(parsing_cycle, "score_and_transition", fake_score)
    monkeypatch.setattr(parsing_cycle, "check_waiting_clusters", fake_waiting)
    monkeypatch.setattr(parsing_cycle, "check_new_clusters", fake_new)
    monkeypatch.setattr(parsing_cycle, "expire_old_clusters", fake_expire)
    monkeypatch.setattr(parsing_cycle, "recover_stuck_clusters", fake_recover)
    monkeypatch.setattr(parsing_cycle, "generate_and_notify", fake_generate)

    health_file = tmp_path / "health"
    result = await parsing_cycle.execute_parsing_cycle(
        max_article_age_hours=36,
        max_cluster_age_hours=24,
        max_concurrent_fetches=3,
        generating_timeout_minutes=30,
        health_file=health_file,
        session_factory=lambda: _cycle_session([source]),
    )

    assert result.fetched == 2
    assert result.passed_filter == 1
    assert result.saved == 1
    assert result.duplicates == 1
    assert result.clusters_affected == 1
    assert result.errors == 0
    assert source.id not in parsing_cycle.consecutive_errors
    assert calls == [
        ("score", {7}),
        ("waiting", 24),
        ("new", 24),
        ("expire", 24),
        ("recover", 30),
        ("generate", 24),
    ]
    assert datetime.fromisoformat(health_file.read_text()) == result.completed_at


async def test_execute_parsing_cycle_records_parser_failure_and_continues(
    monkeypatch,
    tmp_path,
) -> None:
    source = _source()
    updates: list[tuple[int, str]] = []
    calls: list[str] = []
    parsing_cycle.consecutive_errors.clear()

    async def fail_parse_source(
        parsed_source: Source,
        *,
        max_article_age_hours: int,
        parser_factory: object,
        workspace: object = None,
    ) -> tuple[int, int, int, int, set[int]]:
        raise ParserError("boom <timeout>")

    async def fake_update_source_error(source_id: int, error: str) -> None:
        updates.append((source_id, error))

    async def fail_score(cluster_ids: set[int]) -> None:
        raise AssertionError("score should not run without affected clusters")

    async def fake_waiting(*, max_cluster_age_hours: int) -> None:
        calls.append("waiting")

    async def fake_new(*, max_cluster_age_hours: int) -> None:
        calls.append("new")

    async def fake_expire(*, max_cluster_age_hours: int) -> None:
        calls.append("expire")

    async def fake_recover(*, generating_timeout_minutes: int) -> None:
        calls.append("recover")

    async def fake_generate(*, max_cluster_age_hours: int) -> None:
        calls.append("generate")

    monkeypatch.setattr(parsing_cycle, "parse_source", fail_parse_source)
    monkeypatch.setattr(parsing_cycle, "update_source_error", fake_update_source_error)
    monkeypatch.setattr(parsing_cycle, "score_and_transition", fail_score)
    monkeypatch.setattr(parsing_cycle, "check_waiting_clusters", fake_waiting)
    monkeypatch.setattr(parsing_cycle, "check_new_clusters", fake_new)
    monkeypatch.setattr(parsing_cycle, "expire_old_clusters", fake_expire)
    monkeypatch.setattr(parsing_cycle, "recover_stuck_clusters", fake_recover)
    monkeypatch.setattr(parsing_cycle, "generate_and_notify", fake_generate)

    health_file = tmp_path / "health"
    result = await parsing_cycle.execute_parsing_cycle(
        max_article_age_hours=36,
        max_cluster_age_hours=24,
        max_concurrent_fetches=3,
        generating_timeout_minutes=30,
        health_file=health_file,
        session_factory=lambda: _cycle_session([source]),
    )

    assert result.errors == 1
    assert result.fetched == 0
    assert updates == [(source.id, "boom <timeout>")]
    assert source.last_error == "boom <timeout>"
    assert parsing_cycle.consecutive_errors[source.id] == 1
    assert calls == ["waiting", "new", "expire", "recover", "generate"]
    assert health_file.exists()


def test_format_source_error_alert_escapes_source_controlled_text() -> None:
    source = Source(
        id=1,
        name="<b>Feed</b>",
        url="https://example.com/feed.xml",
        type="rss",
        last_error="<timeout>",
    )

    text = parsing_cycle.format_source_error_alert(source, 5)

    assert "&lt;b&gt;Feed&lt;/b&gt;" in text
    assert "&lt;timeout&gt;" in text
    assert "<b>Feed</b>" not in text


async def test_execute_parsing_cycle_stage_failure_still_writes_health_file(
    monkeypatch,
    tmp_path,
) -> None:
    """Regression: an exception in one pipeline stage aborted the cycle before
    the health-file write, flipping Docker health on a single bad cluster."""

    async def fake_stage(**kwargs: object) -> int:
        return 0

    async def failing_generate(**kwargs: object) -> tuple[int, int]:
        raise RuntimeError("boom in generation stage")

    monkeypatch.setattr(parsing_cycle, "check_waiting_clusters", fake_stage)
    monkeypatch.setattr(parsing_cycle, "check_new_clusters", fake_stage)
    monkeypatch.setattr(parsing_cycle, "expire_old_clusters", fake_stage)
    monkeypatch.setattr(parsing_cycle, "recover_stuck_clusters", fake_stage)
    monkeypatch.setattr(parsing_cycle, "generate_and_notify", failing_generate)

    health_file = tmp_path / "health"
    result = await parsing_cycle.execute_parsing_cycle(
        max_article_age_hours=36,
        max_cluster_age_hours=24,
        max_concurrent_fetches=2,
        generating_timeout_minutes=30,
        health_file=health_file,
        session_factory=lambda: _cycle_session([]),
    )

    assert health_file.exists()
    assert datetime.fromisoformat(health_file.read_text()) == result.completed_at
