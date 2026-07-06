from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Optional

import feedparser
import httpx
from loguru import logger

from app.config import settings
from app.database.models import Source
from app.parsers.base import BaseParser, ParsedArticle, ParserError


def _parse_date(entry: dict) -> Optional[datetime]:
    """Try to extract published date from a feedparser entry."""
    for field in ("published", "updated"):
        value = entry.get(field)
        if value:
            try:
                return parsedate_to_datetime(value)
            except Exception:
                pass
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if struct:
        try:
            return datetime(*struct[:6])
        except Exception:
            pass
    return None


from app.parsers.utils import detect_language as _detect_language


def _get_entry_id(entry: dict) -> Optional[str]:
    """Get a unique ID for the feed entry."""
    return entry.get("id") or entry.get("link")


def _get_content(entry: dict) -> Optional[str]:
    """Extract content from feedparser entry, preferring full content over summary."""
    if entry.get("content"):
        return entry["content"][0].get("value", "")
    return entry.get("summary")


class RSSParser(BaseParser):
    """Parser for RSS/Atom feeds.

    Uses httpx for async HTTP (respecting etag/last-modified)
    and feedparser for XML parsing.
    """

    async def parse(self, source: Source) -> list[ParsedArticle]:
        try:
            xml_bytes, new_etag, new_last_modified = await self._fetch_feed(source)
        except Exception as e:
            raise ParserError(f"RSS fetch failed for {source.name}: {e}") from e

        if xml_bytes is None:
            logger.debug(f"RSS feed not modified: {source.name}")
            return []

        feed = feedparser.parse(xml_bytes)
        if feed.bozo and not feed.entries:
            raise ParserError(
                f"RSS parse error for {source.name}: {feed.bozo_exception}"
            )
        if feed.bozo:
            logger.warning(f"RSS parse warning for {source.name}: {feed.bozo_exception}")

        articles = []
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            if not title:
                continue

            link = entry.get("link")
            content = _get_content(entry)
            published = _parse_date(entry)

            lang = _detect_language(title)

            articles.append(
                ParsedArticle(
                    title=title,
                    url=link,
                    content=content,
                    external_id=_get_entry_id(entry),
                    published_at=published,
                    language=lang,
                )
            )

        # Store new etag/last-modified for conditional requests
        source._new_etag = new_etag
        source._new_last_modified = new_last_modified

        logger.info(f"RSS parsed {source.name}: {len(articles)} entries")
        return articles

    async def _fetch_feed(
        self, source: Source
    ) -> tuple[Optional[bytes], Optional[str], Optional[str]]:
        """Fetch RSS feed via httpx with conditional headers.

        Returns (xml_bytes, new_etag, new_last_modified).
        Returns (None, None, None) if feed hasn't changed (304).
        """
        headers = {"User-Agent": "AINewsAggregator/1.0"}
        if source.etag:
            headers["If-None-Match"] = source.etag
        if source.last_modified:
            headers["If-Modified-Since"] = source.last_modified

        async with httpx.AsyncClient() as client:
            response = await client.get(
                source.url,
                headers=headers,
                timeout=settings.request_timeout_seconds,
                follow_redirects=True,
            )

        if response.status_code == 304:
            return None, None, None

        response.raise_for_status()

        new_etag = response.headers.get("ETag")
        new_last_modified = response.headers.get("Last-Modified")

        return response.content, new_etag, new_last_modified
