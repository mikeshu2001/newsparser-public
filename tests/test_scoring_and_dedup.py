from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.database.models import NewsCluster, RawArticle, Source
from app.services.dedup import deduplicate
from app.services.scoring import calculate_score, transition_lifecycle
from app.utils.text import normalize_title


class _ScalarResult:
    def __init__(self, items: list[object]):
        self._items = items

    def all(self) -> list[object]:
        return self._items


class _FakeSession:
    def __init__(self, *result_sets: list[object]):
        self._result_sets = list(result_sets)
        self.added: list[object] = []

    async def scalars(self, statement: object) -> _ScalarResult:
        return _ScalarResult(self._result_sets.pop(0))

    def add(self, item: object) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        for index, item in enumerate(self.added, start=1000):
            if getattr(item, "id", None) is None:
                item.id = index


def _cluster() -> NewsCluster:
    return NewsCluster(
        id=10,
        topic=normalize_title("OpenAI launches GPT-5"),
        topic_original="OpenAI launches GPT-5",
        first_seen_at=datetime.now(timezone.utc),
        sources_count=99,
    )


def _article(source_id: int) -> RawArticle:
    return RawArticle(
        id=source_id,
        source_id=source_id,
        external_id=f"https://example.com/{source_id}",
        title="OpenAI launches GPT-5",
        content="",
        language="en",
        cluster_id=10,
        fetched_at=None,
    )


async def test_calculate_score_uses_distinct_source_count_for_cross_refs() -> None:
    cluster = _cluster()
    articles = [_article(1), _article(1), _article(1)]
    sources = [Source(id=1, name="Source 1", url="https://s1.test", type="rss", weight=5)]
    session = _FakeSession(articles, sources)

    score = await calculate_score(session, cluster)

    assert score == 15
    assert cluster.sources_count == 1


async def test_calculate_score_rewards_distinct_sources() -> None:
    cluster = _cluster()
    articles = [_article(1), _article(2), _article(3)]
    sources = [
        Source(id=1, name="Source 1", url="https://s1.test", type="rss", weight=5),
        Source(id=2, name="Source 2", url="https://s2.test", type="rss", weight=5),
        Source(id=3, name="Source 3", url="https://s3.test", type="rss", weight=5),
    ]
    session = _FakeSession(articles, sources)

    score = await calculate_score(session, cluster)

    assert score == 45
    assert cluster.sources_count == 3


async def test_deduplicate_does_not_blindly_increment_sources_count() -> None:
    cluster = _cluster()
    cluster.sources_count = 1
    article = RawArticle(
        id=100,
        source_id=2,
        external_id="https://example.com/100",
        title="OpenAI launches GPT-5",
        content="",
        language="en",
    )
    session = _FakeSession([cluster])

    cluster_id = await deduplicate(session, article, 1)

    assert cluster_id == cluster.id
    assert article.cluster_id == cluster.id
    assert cluster.sources_count == 1


async def test_deduplicate_single_shared_entity_with_unrelated_title_creates_new_cluster() -> None:
    """Regression: one shared brand ('openai') must not merge unrelated stories."""
    cluster = _cluster()
    session = _FakeSession([cluster], [])  # active window, then approved window
    article = RawArticle(
        id=101,
        source_id=2,
        external_id="https://example.com/101",
        title="OpenAI hires new chief marketing officer",
        content="",
        language="en",
    )

    cluster_id = await deduplicate(session, article, 1)

    assert cluster_id is not None
    assert cluster_id != cluster.id
    assert session.added, "expected a new cluster instead of a brand-only merge"


async def test_deduplicate_same_brand_story_not_suppressed_by_approved_cluster() -> None:
    """Regression: approved-cooldown silently dropped new same-brand stories."""
    approved = _cluster()
    approved.status = "approved"
    session = _FakeSession([], [approved])
    article = RawArticle(
        id=102,
        source_id=2,
        external_id="https://example.com/102",
        title="OpenAI hires new chief marketing officer",
        content="",
        language="en",
    )

    cluster_id = await deduplicate(session, article, 1)

    assert cluster_id is not None


def test_match_score_single_entity_requires_title_corroboration() -> None:
    from rapidfuzz import fuzz

    from app.services.dedup import (
        ENTITY_CORROBORATION_FUZZY,
        FUZZY_THRESHOLD,
        _match_score,
    )

    cluster = _cluster()  # topic: "openai launches gpt 5"

    unrelated = normalize_title("OpenAI hires new chief marketing officer")
    assert _match_score(unrelated, {"openai"}, cluster) == 0

    corroborated = normalize_title("OpenAI launches new product")
    ratio = fuzz.token_sort_ratio(corroborated, cluster.topic)
    assert ENTITY_CORROBORATION_FUZZY <= ratio < FUZZY_THRESHOLD, ratio
    assert _match_score(corroborated, {"openai"}, cluster) > 0


async def test_transition_lifecycle_refreshes_updated_at_for_hot_generating() -> None:
    old_updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
    cluster = _cluster()
    cluster.status = "new"
    cluster.score = 90
    cluster.updated_at = old_updated_at

    await transition_lifecycle(
        session=object(),  # type: ignore[arg-type]
        cluster=cluster,
        score_threshold=50,
        hot_threshold=80,
    )

    assert cluster.status == "generating"
    assert cluster.updated_at > old_updated_at


async def test_transition_lifecycle_refreshes_updated_at_for_waiting_generating() -> None:
    old_updated_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    cluster = _cluster()
    cluster.status = "waiting"
    cluster.score = 60
    cluster.updated_at = old_updated_at
    cluster.status_changed_at = old_updated_at

    await transition_lifecycle(
        session=object(),  # type: ignore[arg-type]
        cluster=cluster,
        score_threshold=50,
        hot_threshold=80,
        cluster_wait_minutes=30,
    )

    assert cluster.status == "generating"
    assert cluster.updated_at > old_updated_at
    assert cluster.status_changed_at > old_updated_at


async def test_transition_lifecycle_waiting_clock_ignores_updated_at_churn() -> None:
    """Regression: dedup/scoring bump updated_at on every matched article; the
    waiting delay must be measured from entering 'waiting', not last activity."""
    now = datetime.now(timezone.utc)
    cluster = _cluster()
    cluster.status = "waiting"
    cluster.score = 60
    cluster.updated_at = now - timedelta(minutes=31)
    cluster.status_changed_at = now - timedelta(minutes=5)

    await transition_lifecycle(
        session=object(),  # type: ignore[arg-type]
        cluster=cluster,
        score_threshold=50,
        hot_threshold=80,
        cluster_wait_minutes=30,
    )

    assert cluster.status == "waiting"
