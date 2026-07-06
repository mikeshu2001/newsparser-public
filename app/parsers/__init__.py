from app.parsers.base import BaseParser
from app.parsers.rss_parser import RSSParser
from app.parsers.sitemap_parser import SitemapParser
from app.parsers.web_scraper import WebScraperParser

_PARSERS: dict[str, BaseParser] = {
    "rss": RSSParser(),
    "telegram": RSSParser(),  # TG channels via tg.i-c-a.su are regular RSS
    "web": WebScraperParser(),
    "sitemap": SitemapParser(),
}


def get_parser(source_type: str) -> BaseParser:
    """Return appropriate parser for the given source type."""
    parser = _PARSERS.get(source_type)
    if parser is None:
        raise ValueError(f"Unknown source type: {source_type}")
    return parser
