from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Callable

from loguru import logger
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database.database import async_session
from app.database.models import RawArticle, Source, Workspace
from app.parsers import get_parser
from app.parsers.base import ParsedArticle, ParserError
from app.services.dedup import deduplicate
from app.services.filter import is_relevant


def is_article_fresh(
    article: ParsedArticle,
    *,
    max_article_age_hours: int,
    now: datetime | None = None,
) -> bool:
    """Return True when an article is fresh enough for ingestion."""
    if article.published_at is None:
        return True

    pub = article.published_at
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    current_time = now or datetime.now(timezone.utc)
    age = current_time - pub
    return age.total_seconds() <= max_article_age_hours * 3600


def normalize_published_at(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


_MAX_ID_LENGTH = 500  # raw_articles.external_id / url are VARCHAR(500)


def _stable_external_id(article: ParsedArticle) -> str:
    external_id = (article.external_id or "").strip()
    if external_id and len(external_id) <= _MAX_ID_LENGTH:
        return external_id

    url = (article.url or "").strip()
    if url and len(url) <= _MAX_ID_LENGTH:
        return url

    published_at = normalize_published_at(article.published_at)
    # Over-long ids/urls fall through to the digest and are included in it so
    # two distinct articles never share a fingerprint. Articles without
    # id/url keep the legacy composition (digests stay stable).
    parts = [
        article.title.strip(),
        published_at.isoformat() if published_at else "",
    ]
    if external_id:
        parts.append(external_id)
    if url:
        parts.append(url)
    parts.append((article.content or "").strip())
    fingerprint = "\n".join(parts)
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return f"generated:{digest}"


def _raw_article_values(source: Source, article: ParsedArticle) -> dict[str, object]:
    return {
        "source_id": source.id,
        "external_id": _stable_external_id(article),
        "title": article.title,
        "content": article.content,
        "url": article.url[:_MAX_ID_LENGTH] if article.url else article.url,
        "language": article.language,
        "published_at": normalize_published_at(article.published_at),
    }


async def parse_source(
    source: Source,
    *,
    max_article_age_hours: int,
    parser_factory: Callable[[str], object] = get_parser,
    workspace: Workspace | None = None,
) -> tuple[int, int, int, int, set[int]]:
    """Parse a source and persist relevant fresh articles.

    Returns (fetched, passed_filter, saved_new, duplicates, cluster_ids).
    """
    try:
        parser = parser_factory(source.type)
    except ValueError as e:
        raise ParserError(f"Unsupported source type for {source.name}: {e}") from e

    articles = await parser.parse(source)
    fetched = len(articles)

    if fetched == 0:
        await update_source_checked(source)
        return 0, 0, 0, 0, set()

    articles = [
        article
        for article in articles
        if is_article_fresh(
            article,
            max_article_age_hours=max_article_age_hours,
        )
    ]

    relevant = [
        article
        for article in articles
        if is_relevant(article, source.weight, workspace)
    ]
    passed = len(relevant)

    saved, dupes, cluster_ids = await save_and_dedup(source, relevant)
    await update_source_checked(source)

    logger.info(
        f"Source {source.name}: fetched {fetched}, "
        f"passed filter {passed}, saved {saved} new, {dupes} duplicates"
    )

    return fetched, passed, saved, dupes, cluster_ids


async def save_and_dedup(
    source: Source,
    articles: list[ParsedArticle],
) -> tuple[int, int, set[int]]:
    """Save articles to raw_articles, then deduplicate new ones into clusters."""
    if not articles:
        return 0, 0, set()

    saved = 0
    dupes = 0
    cluster_ids: set[int] = set()

    async with async_session() as session:
        for article in articles:
            stmt = (
                pg_insert(RawArticle)
                .values(**_raw_article_values(source, article))
                .on_conflict_do_nothing(
                    index_elements=["source_id", "external_id"],
                )
                .returning(RawArticle)
            )
            raw_article = (await session.scalars(stmt)).first()
            if raw_article:
                saved += 1
                cluster_id = await deduplicate(
                    session, raw_article, source.workspace_id
                )
                if cluster_id is not None:
                    cluster_ids.add(cluster_id)
            else:
                dupes += 1

        await session.commit()

    return saved, dupes, cluster_ids


async def update_source_checked(source: Source) -> None:
    """Update source.last_checked_at and etag/last_modified if changed."""
    async with async_session() as session:
        db_source = await session.get(Source, source.id)
        if db_source:
            db_source.last_checked_at = datetime.now(timezone.utc)
            db_source.last_error = None

            new_etag = getattr(source, "_new_etag", None)
            new_last_modified = getattr(source, "_new_last_modified", None)
            if new_etag:
                db_source.etag = new_etag
            if new_last_modified:
                db_source.last_modified = new_last_modified

            await session.commit()


async def update_source_error(source_id: int, error: str) -> None:
    """Record parsing error on the source."""
    async with async_session() as session:
        db_source = await session.get(Source, source_id)
        if db_source:
            db_source.last_checked_at = datetime.now(timezone.utc)
            db_source.last_error = error[:500]
            await session.commit()
