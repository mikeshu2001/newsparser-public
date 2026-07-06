from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.database.models import Source
from app.parsers.base import ParserError
from app.parsers.base import ParsedArticle
from app.parsers.rss_parser import RSSParser
from app.parsers.sitemap_parser import SitemapParser
from app.parsers.web_scraper import WebScraperParser
from app.services import ingestion


def _source(source_type: str = "rss", **kwargs: object) -> Source:
    return Source(
        id=1,
        name=str(kwargs.get("name", "Test Source")),
        url=str(kwargs.get("url", "https://example.com/feed")),
        type=source_type,
        scraper_config=kwargs.get("scraper_config"),
    )


async def test_rss_fetch_failure_raises_parser_error(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = RSSParser()

    async def fail_fetch(source: Source) -> tuple[bytes | None, str | None, str | None]:
        raise RuntimeError("network down")

    monkeypatch.setattr(parser, "_fetch_feed", fail_fetch)

    with pytest.raises(ParserError, match="RSS fetch failed"):
        await parser.parse(_source())


async def test_rss_bozo_without_entries_raises_parser_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = RSSParser()

    async def fetch(source: Source) -> tuple[bytes | None, str | None, str | None]:
        return b"bad xml", None, None

    monkeypatch.setattr(parser, "_fetch_feed", fetch)
    monkeypatch.setattr(
        "app.parsers.rss_parser.feedparser.parse",
        lambda _: SimpleNamespace(
            bozo=True,
            entries=[],
            bozo_exception=ValueError("bad xml"),
        ),
    )

    with pytest.raises(ParserError, match="RSS parse error"):
        await parser.parse(_source())


async def test_web_fetch_failure_raises_parser_error(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = WebScraperParser()

    async def fail_fetch(url: str) -> str:
        raise RuntimeError("blocked")

    monkeypatch.setattr(parser, "_fetch_page", fail_fetch)
    source = _source(
        "web",
        scraper_config={
            "list_url": "https://example.com/news",
            "article_selector": "a.article",
        },
    )

    with pytest.raises(ParserError, match="Web fetch failed"):
        await parser.parse(source)


async def test_sitemap_fetch_failure_raises_parser_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = SitemapParser()

    async def fail_fetch(sitemap_url: str, url_filter: str) -> list[str]:
        raise RuntimeError("invalid xml")

    monkeypatch.setattr(parser, "_fetch_sitemap", fail_fetch)
    source = _source(
        "sitemap",
        scraper_config={"sitemap_url": "https://example.com/sitemap.xml"},
    )

    with pytest.raises(ParserError, match="Sitemap fetch failed"):
        await parser.parse(source)


async def test_sitemap_all_page_fetch_failures_raise_parser_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = SitemapParser()

    async def fetch_sitemap(
        sitemap_url: str, url_filter: str
    ) -> list[tuple[str, None]]:
        return [
            ("https://example.com/news/a", None),
            ("https://example.com/news/b", None),
        ]

    async def fetch_page_meta(
        client: object, url: str, published_at: object = None
    ) -> ParsedArticle | None:
        raise ParserError(f"Failed to fetch sitemap page {url}: blocked")

    monkeypatch.setattr(parser, "_fetch_sitemap", fetch_sitemap)
    monkeypatch.setattr(parser, "_fetch_page_meta", fetch_page_meta)
    source = _source(
        "sitemap",
        scraper_config={"sitemap_url": "https://example.com/sitemap.xml"},
    )

    with pytest.raises(ParserError, match="produced no usable articles"):
        await parser.parse(source)


async def test_sitemap_zero_yield_with_partial_failures_raises_parser_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: one meta-less page + failures used to return [] silently."""
    parser = SitemapParser()

    async def fetch_sitemap(
        sitemap_url: str, url_filter: str
    ) -> list[tuple[str, None]]:
        return [
            ("https://example.com/news/a", None),
            ("https://example.com/news/b", None),
        ]

    async def fetch_page_meta(
        client: object, url: str, published_at: object = None
    ) -> ParsedArticle | None:
        if url.endswith("/b"):
            raise ParserError(f"Failed to fetch sitemap page {url}: blocked")
        return None  # page without a usable title

    monkeypatch.setattr(parser, "_fetch_sitemap", fetch_sitemap)
    monkeypatch.setattr(parser, "_fetch_page_meta", fetch_page_meta)
    source = _source(
        "sitemap",
        scraper_config={"sitemap_url": "https://example.com/sitemap.xml"},
    )

    with pytest.raises(ParserError, match="produced no usable articles"):
        await parser.parse(source)


async def test_sitemap_partial_page_fetch_failures_still_return_articles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = SitemapParser()

    async def fetch_sitemap(
        sitemap_url: str, url_filter: str
    ) -> list[tuple[str, None]]:
        return [
            ("https://example.com/news/a", None),
            ("https://example.com/news/b", None),
        ]

    async def fetch_page_meta(
        client: object, url: str, published_at: object = None
    ) -> ParsedArticle | None:
        if url.endswith("/b"):
            raise ParserError(f"Failed to fetch sitemap page {url}: blocked")
        return ParsedArticle(
            title="Working page",
            url=url,
            content=None,
            external_id=url,
            published_at=None,
            language="en",
        )

    monkeypatch.setattr(parser, "_fetch_sitemap", fetch_sitemap)
    monkeypatch.setattr(parser, "_fetch_page_meta", fetch_page_meta)
    source = _source(
        "sitemap",
        scraper_config={"sitemap_url": "https://example.com/sitemap.xml"},
    )

    articles = await parser.parse(source)

    assert [article.title for article in articles] == ["Working page"]


async def test_sitemap_fetches_newest_lastmod_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: oldest-first assumption skipped new posts on newest-first
    sitemaps; <lastmod> ordering also feeds published_at into the freshness
    filter (it was always None before)."""
    from datetime import datetime, timezone

    parser = SitemapParser()
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    new = datetime(2026, 7, 1, tzinfo=timezone.utc)
    fetched: list[tuple[str, object]] = []

    async def fetch_sitemap(
        sitemap_url: str, url_filter: str
    ) -> list[tuple[str, object]]:
        return [
            ("https://example.com/news/old", old),
            ("https://example.com/news/new", new),
        ]

    async def fetch_page_meta(
        client: object, url: str, published_at: object = None
    ) -> ParsedArticle | None:
        fetched.append((url, published_at))
        return ParsedArticle(
            title=url,
            url=url,
            content=None,
            external_id=url,
            published_at=published_at,
            language="en",
        )

    monkeypatch.setattr(parser, "_fetch_sitemap", fetch_sitemap)
    monkeypatch.setattr(parser, "_fetch_page_meta", fetch_page_meta)
    source = _source(
        "sitemap",
        scraper_config={"sitemap_url": "https://example.com/sitemap.xml"},
    )

    articles = await parser.parse(source)

    assert fetched[0] == ("https://example.com/news/new", new)
    assert articles[0].published_at == new


async def test_ingestion_parse_source_does_not_mark_parser_failure_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingParser:
        async def parse(self, source: Source) -> list[object]:
            raise ParserError("source is broken")

    checked = False

    async def update_checked(source: Source) -> None:
        nonlocal checked
        checked = True

    monkeypatch.setattr(ingestion, "update_source_checked", update_checked)

    with pytest.raises(ParserError, match="source is broken"):
        await ingestion.parse_source(
            _source(),
            max_article_age_hours=36,
            parser_factory=lambda source_type: FailingParser(),
        )

    assert checked is False
