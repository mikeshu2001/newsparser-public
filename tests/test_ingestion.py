from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.database.models import Source
from app.parsers.base import ParsedArticle
from app.services.ingestion import (
    _raw_article_values,
    _stable_external_id,
    is_article_fresh,
    normalize_published_at,
)


def _source() -> Source:
    return Source(id=10, name="Feed", url="https://example.com", type="rss")


def test_is_article_fresh_accepts_unknown_published_date() -> None:
    article = ParsedArticle(title="No date")

    assert is_article_fresh(
        article,
        max_article_age_hours=36,
        now=datetime(2026, 6, 28, tzinfo=timezone.utc),
    )


def test_is_article_fresh_accepts_recent_timezone_aware_date() -> None:
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
    article = ParsedArticle(title="Recent", published_at=now - timedelta(hours=2))

    assert is_article_fresh(article, max_article_age_hours=36, now=now)


def test_is_article_fresh_rejects_old_timezone_aware_date() -> None:
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
    article = ParsedArticle(title="Old", published_at=now - timedelta(hours=40))

    assert not is_article_fresh(article, max_article_age_hours=36, now=now)


def test_is_article_fresh_treats_naive_date_as_utc() -> None:
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
    article = ParsedArticle(
        title="Naive",
        published_at=datetime(2026, 6, 28, 10, 0),
    )

    assert is_article_fresh(article, max_article_age_hours=3, now=now)


def test_normalize_published_at_preserves_missing_date() -> None:
    assert normalize_published_at(None) is None


def test_normalize_published_at_treats_naive_date_as_utc() -> None:
    value = datetime(2026, 6, 28, 10, 30)

    assert normalize_published_at(value) == datetime(
        2026,
        6,
        28,
        10,
        30,
        tzinfo=timezone.utc,
    )


def test_normalize_published_at_converts_aware_date_to_utc() -> None:
    value = datetime(
        2026,
        6,
        28,
        13,
        30,
        tzinfo=timezone(timedelta(hours=3)),
    )

    assert normalize_published_at(value) == datetime(
        2026,
        6,
        28,
        10,
        30,
        tzinfo=timezone.utc,
    )


def test_stable_external_id_prefers_parser_id() -> None:
    article = ParsedArticle(
        title="Title",
        url="https://example.com/article",
        external_id=" feed-guid ",
    )

    assert _stable_external_id(article) == "feed-guid"


def test_stable_external_id_falls_back_to_url() -> None:
    article = ParsedArticle(
        title="Title",
        url=" https://example.com/article ",
        external_id=None,
    )

    assert _stable_external_id(article) == "https://example.com/article"


def test_stable_external_id_generates_stable_hash_without_id_or_url() -> None:
    article = ParsedArticle(
        title="Same title",
        content="Same content",
        published_at=datetime(2026, 6, 28, 9, 0),
    )

    first = _stable_external_id(article)
    second = _stable_external_id(article)

    assert first == second
    assert first.startswith("generated:")


def test_stable_external_id_hash_changes_for_different_article_body() -> None:
    first = ParsedArticle(title="Title", content="First")
    second = ParsedArticle(title="Title", content="Second")

    assert _stable_external_id(first) != _stable_external_id(second)


def test_raw_article_values_never_use_null_external_id() -> None:
    article = ParsedArticle(
        title="Title",
        content="Body",
        published_at=datetime(
            2026,
            6,
            28,
            13,
            30,
            tzinfo=timezone(timedelta(hours=3)),
        ),
    )

    values = _raw_article_values(_source(), article)

    assert values["source_id"] == 10
    assert isinstance(values["external_id"], str)
    assert values["external_id"]
    assert values["published_at"] == datetime(
        2026,
        6,
        28,
        10,
        30,
        tzinfo=timezone.utc,
    )
