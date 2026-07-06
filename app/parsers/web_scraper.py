from __future__ import annotations

from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from app.config import settings
from app.database.models import Source
from app.parsers.base import BaseParser, ParsedArticle, ParserError
from app.parsers.utils import (
    BROWSER_USER_AGENT,
    detect_language as _detect_language,
    extract_html_title as _extract_html_title,
    extract_meta as _extract_meta,
)

_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class WebScraperParser(BaseParser):
    """Parser for sources without RSS — uses httpx + BeautifulSoup.

    Reads CSS selectors from source.scraper_config (JSONB):
      {
        "list_url": "https://...",          # page with article links
        "article_selector": "a[href*=...]", # CSS selector for article links
        "title_selector": "h2",             # title within card (optional with fetch_meta)
        "fetch_meta": true,                 # fetch og:title from each article page
        "date_selector": "time",            # date element (optional)
        "date_format": "%Y-%m-%d",          # strptime format (optional)
      }

    When "fetch_meta" is true (recommended):
    - URLs are discovered from the listing page (real-time, new articles appear immediately)
    - Title and description are fetched from each article's og:title/og:description
    - This is robust: og:title lives in <head> and survives redesigns
    - Falls back to card text if article page fetch fails
    """

    async def parse(self, source: Source) -> list[ParsedArticle]:
        config = source.scraper_config
        if not config:
            raise ParserError(f"No scraper_config for web source: {source.name}")

        list_url = config.get("list_url", source.url)
        article_selector = config.get("article_selector")
        fetch_meta = config.get("fetch_meta", False)

        if not article_selector:
            raise ParserError(f"No article_selector in scraper_config: {source.name}")

        # Step 1: Fetch listing page and extract article URLs + fallback titles
        try:
            list_html = await self._fetch_page(list_url)
        except Exception as e:
            raise ParserError(f"Web fetch failed for {source.name}: {e}") from e

        soup = BeautifulSoup(list_html, "html.parser")
        link_elements = soup.select(article_selector)

        if not link_elements:
            raise ParserError(f"Web scraper found 0 articles for {source.name}")

        # Extract URLs and fallback titles from listing page
        candidates = []
        seen_urls = set()
        title_selector = config.get("title_selector")

        for el in link_elements:
            href = el.get("href")
            if not href:
                continue
            article_url = urljoin(list_url, href)

            if article_url in seen_urls:
                continue
            seen_urls.add(article_url)

            # Fallback title from card element
            fallback_title = ""
            if title_selector:
                title_el = el.select_one(title_selector)
                if not title_el and el.parent:
                    title_el = el.parent.select_one(title_selector)
                if title_el:
                    fallback_title = title_el.get_text(strip=True)
            if not fallback_title:
                fallback_title = el.get_text(" ", strip=True)

            published = self._extract_date(el, config)
            candidates.append((article_url, fallback_title, published))

        # Step 2: If fetch_meta enabled, get og:title from each article page
        if fetch_meta and candidates:
            articles = await self._fetch_articles_meta(candidates)
        else:
            articles = []
            for article_url, title, published in candidates:
                if not title:
                    continue
                articles.append(
                    ParsedArticle(
                        title=title,
                        url=article_url,
                        content=None,
                        external_id=article_url,
                        published_at=published,
                        language=_detect_language(title),
                    )
                )

        logger.info(f"Web scraped {source.name}: {len(articles)} articles")
        return articles

    async def _fetch_articles_meta(
        self, candidates: list[tuple[str, str, Optional[datetime]]]
    ) -> list[ParsedArticle]:
        """Fetch og:title and og:description from each article page."""
        articles = []
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            headers=_HEADERS,
        ) as client:
            for article_url, fallback_title, published in candidates:
                try:
                    resp = await client.get(article_url)
                    resp.raise_for_status()
                    page_html = resp.text

                    title = _extract_meta(page_html, "og:title")
                    if not title:
                        title = _extract_html_title(page_html)
                    if not title:
                        title = fallback_title

                    # Clean site name suffixes
                    if title:
                        for sep in (" | ", " — ", " - ", " – "):
                            if sep in title:
                                title = title.split(sep)[0].strip()

                    description = _extract_meta(page_html, "og:description")
                except Exception as e:
                    logger.debug(f"Failed to fetch meta for {article_url}: {e}")
                    title = fallback_title
                    description = None

                if not title:
                    continue

                articles.append(
                    ParsedArticle(
                        title=title,
                        url=article_url,
                        content=description,
                        external_id=article_url,
                        published_at=published,
                        language=_detect_language(title),
                    )
                )

        return articles

    def _extract_date(self, el: object, config: dict) -> Optional[datetime]:
        """Try to extract date from element using config selectors."""
        date_selector = config.get("date_selector")
        date_format = config.get("date_format")
        if not date_selector:
            return None

        date_el = el.select_one(date_selector)
        if not date_el:
            return None

        date_text = date_el.get("datetime") or date_el.get_text(strip=True)
        if not date_text:
            return None

        if date_format:
            try:
                return datetime.strptime(date_text, date_format)
            except ValueError:
                pass

        try:
            return datetime.fromisoformat(date_text.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    async def _fetch_page(self, url: str) -> str:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers=_HEADERS,
                timeout=settings.request_timeout_seconds,
                follow_redirects=True,
            )
        response.raise_for_status()
        return response.text
