from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.database.models import GeneratedArticle, NewsCluster
from app.services import generation_pipeline
from app.services.notifier import DeliveryResult


class _ScalarResult:
    def __init__(self, items: list[object]):
        self._items = items

    def all(self) -> list[object]:
        return self._items


class _FakeSession:
    def __init__(self, clusters: list[NewsCluster]):
        self.clusters = clusters
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def scalars(self, statement: object) -> _ScalarResult:
        return _ScalarResult(self.clusters)

    async def get(self, model: object, cluster_id: int) -> NewsCluster | None:
        for cluster in self.clusters:
            if cluster.id == cluster_id:
                return cluster
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _cluster(cluster_id: int) -> NewsCluster:
    return NewsCluster(
        id=cluster_id,
        status="generating",
        first_seen_at=datetime.now(timezone.utc),
    )


def _session_factory(session: _FakeSession):
    return lambda: session


async def test_generate_and_notify_reverts_to_waiting_when_generation_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = _cluster(1)
    session = _FakeSession([cluster])

    async def generate(session_obj: object, cluster_obj: NewsCluster) -> None:
        return None

    monkeypatch.setattr(generation_pipeline, "generate_article", generate)

    result = await generation_pipeline.generate_and_notify(
        session_factory=_session_factory(session),
    )

    assert result == (0, 0)
    assert cluster.status == "waiting"
    assert session.commits == 1
    assert session.rollbacks == 0


async def test_generate_and_notify_skips_manually_claimed_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = _cluster(1)
    session = _FakeSession([cluster])
    generation_pipeline.mark_manual_generation(cluster.id)

    async def generate(
        session_obj: object,
        cluster_obj: NewsCluster,
    ) -> GeneratedArticle:
        raise AssertionError("claimed manual generation should be skipped")

    monkeypatch.setattr(generation_pipeline, "generate_article", generate)

    try:
        result = await generation_pipeline.generate_and_notify(
            session_factory=_session_factory(session),
        )
    finally:
        generation_pipeline.unmark_manual_generation(cluster.id)

    assert result == (0, 0)
    assert cluster.status == "generating"
    assert session.commits == 0
    assert session.rollbacks == 0


async def test_generate_and_notify_counts_zero_delivery_as_undelivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = _cluster(2)
    session = _FakeSession([cluster])

    async def generate(
        session_obj: object,
        cluster_obj: NewsCluster,
    ) -> GeneratedArticle:
        cluster_obj.status = "pending_review"
        return GeneratedArticle(id=10, cluster_id=cluster_obj.id, headline="H", body="B")

    async def notify(
        session_obj: object,
        article: GeneratedArticle,
        cluster_obj: NewsCluster,
        workspace: object = None,
    ) -> DeliveryResult:
        return DeliveryResult(total_recipients=1, delivered_count=0, failed_count=1)

    monkeypatch.setattr(generation_pipeline, "generate_article", generate)
    monkeypatch.setattr(generation_pipeline, "send_to_moderators", notify)

    result = await generation_pipeline.generate_and_notify(
        session_factory=_session_factory(session),
    )

    assert result == (0, 1)
    assert cluster.status == "pending_review"
    assert session.commits == 2
    assert session.rollbacks == 0


async def test_generate_and_notify_counts_successful_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = _cluster(3)
    session = _FakeSession([cluster])

    async def generate(
        session_obj: object,
        cluster_obj: NewsCluster,
    ) -> GeneratedArticle:
        cluster_obj.status = "pending_review"
        return GeneratedArticle(id=11, cluster_id=cluster_obj.id, headline="H", body="B")

    async def notify(
        session_obj: object,
        article: GeneratedArticle,
        cluster_obj: NewsCluster,
        workspace: object = None,
    ) -> DeliveryResult:
        return DeliveryResult(total_recipients=1, delivered_count=1, failed_count=0)

    monkeypatch.setattr(generation_pipeline, "generate_article", generate)
    monkeypatch.setattr(generation_pipeline, "send_to_moderators", notify)

    result = await generation_pipeline.generate_and_notify(
        session_factory=_session_factory(session),
    )

    assert result == (1, 0)
    assert session.commits == 2
    assert session.rollbacks == 0


async def test_generate_and_notify_rolls_back_exception_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _cluster(1)
    second = _cluster(2)
    session = _FakeSession([first, second])

    async def generate(
        session_obj: object,
        cluster_obj: NewsCluster,
    ) -> GeneratedArticle:
        if cluster_obj.id == 1:
            raise RuntimeError("AI down")
        cluster_obj.status = "pending_review"
        return GeneratedArticle(id=12, cluster_id=cluster_obj.id, headline="H", body="B")

    async def notify(
        session_obj: object,
        article: GeneratedArticle,
        cluster_obj: NewsCluster,
        workspace: object = None,
    ) -> DeliveryResult:
        return DeliveryResult(total_recipients=1, delivered_count=1, failed_count=0)

    monkeypatch.setattr(generation_pipeline, "generate_article", generate)
    monkeypatch.setattr(generation_pipeline, "send_to_moderators", notify)

    result = await generation_pipeline.generate_and_notify(
        session_factory=_session_factory(session),
    )

    assert result == (1, 0)
    assert session.rollbacks == 1
    assert session.commits == 2
