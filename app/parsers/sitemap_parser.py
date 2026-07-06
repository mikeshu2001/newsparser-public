from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

import httpx
from loguru import logger

from app.config import settings
from app.database.models import Source
from app.parsers.base import BaseParser, ParsedArticle, ParserError
from app.parsers.utils import (
    BROWSER_USER_AGENT,
    detect_language as _detect_language,
    extract_html_title,
    extract_meta,
)

# Max new articles to fetch per cycle (avoid hammering the site on first run)
_MAX_NEW_PAGES = 20


class SitemapParser(BaseParser):
    """Parser that discovers articles via sitemap.xml.

    Much more reliable than CSS-selector scraping:
    - sitemap.xml is maintained for SEO and rarely changes structure
    - Title/description extracted from <title> and og:title meta tags
      which are in <head> and survive redesigns

    source.scraper_config should contain:
      {
        "sitemap_url": "https://example.com/sitemap.xml",
        "url_contains": "/news/",  # filter URLs by this substring
      }
    """

    async def parse(self, source: Source) -> list[ParsedArticle]:
        config = source.scraper_config or {}
        sitemap_url = config.get("sitemap_url")
        url_filter = config.get("url_contains", "")

        if not sitemap_url:
            raise ParserError(f"No sitemap_url in scraper_config: {source.name}")

        # Step 1: Fetch and parse sitemap.xml
        try:
            entries = await self._fetch_sitemap(sitemap_url, url_filter)
        except Exception as e:
            raise ParserError(f"Sitemap fetch failed for {source.name}: {e}") from e

        if not entries:
            raise ParserError(f"Sitemap has 0 matching URLs: {source.name}")

        logger.debug(f"Sitemap {source.name}: {len(entries)} URLs found")

        # Step 2: Fetch metadata for the newest N URLs (the DB constraint
        # dedups re-fetches). Prefer <lastmod> ordering; without it fall back
        # to the legacy assumption that sitemaps are oldest-first.
        if any(lastmod for _url, lastmod in entries):
            entries.sort(
                key=lambda item: item[1]
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            candidates = entries[:_MAX_NEW_PAGES]
        else:
            candidates = list(reversed(entries))[:_MAX_NEW_PAGES]

        articles = []
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": BROWSER_USER_AGENT},
        ) as client:
            page_fetch_failures = 0
            for url, lastmod in candidates:
                try:
                    meta = await self._fetch_page_meta(client, url, lastmod)
                except ParserError as e:
                    page_fetch_failures += 1
                    logger.debug(str(e))
                    continue
                if meta:
                    articles.append(meta)

        if not articles:
            # Explicit failure instead of a silent empty feed: pages were
            # listed but none produced a usable article.
            raise ParserError(
                f"Sitemap produced no usable articles for {source.name}: "
                f"{page_fetch_failures}/{len(candidates)} page fetches failed"
            )

        logger.info(f"Sitemap parsed {source.name}: {len(articles)} articles")
        return articles

    async def _fetch_sitemap(
        self, sitemap_url: str, url_filter: str
    ) -> list[tuple[str, Optional[datetime]]]:
        """Fetch sitemap.xml and return filtered (url, lastmod) entries."""
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "AINewsAggregator/1.0"},
        ) as client:
            response = await client.get(sitemap_url)
        response.raise_for_status()

        root = ET.fromstring(response.content)
        # Handle XML namespace
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        entries: list[tuple[str, Optional[datetime]]] = []
        for url_el in root.iter(f"{ns}url"):
            loc = url_el.find(f"{ns}loc")
            if loc is not None and loc.text:
                article_url = loc.text.strip()
                if url_filter and url_filter not in article_url:
                    continue
                lastmod_el = url_el.find(f"{ns}lastmod")
                lastmod = _parse_lastmod(
                    lastmod_el.text if lastmod_el is not None else None
                )
                entries.append((article_url, lastmod))

        return entries

    async def _fetch_page_meta(
        self,
        client: httpx.AsyncClient,
        url: str,
        published_at: Optional[datetime] = None,
    ) -> Optional[ParsedArticle]:
        """Fetch a page and extract title from <title> / og:title."""
        try:
            response = await client.get(url)
            response.raise_for_status()
        except Exception as e:
            raise ParserError(f"Failed to fetch sitemap page {url}: {e}") from e

        page_html = response.text

        # Extract og:title (most reliable for articles)
        title = extract_meta(page_html, "og:title")
        if not title:
            title = extract_html_title(page_html)
        if not title:
            return None

        # Extract og:description for content preview
        description = extract_meta(page_html, "og:description")

        lang = _detect_language(title)

        return ParsedArticle(
            title=title,
            url=url,
            content=description,
            external_id=url,
            published_at=published_at,
            language=lang,
        )


def _parse_lastmod(value: Optional[str]) -> Optional[datetime]:
    """Parse a sitemap <lastmod> value into an aware datetime, or None."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
