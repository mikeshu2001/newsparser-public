from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from app.database.models import Source


class ParserError(Exception):
    """Raised when a source cannot be fetched or parsed reliably."""


class ParsedArticle:
    """Parsed article data before saving to DB."""

    __slots__ = ("title", "content", "url", "external_id", "published_at", "language")

    def __init__(
        self,
        title: str,
        url: Optional[str] = None,
        content: Optional[str] = None,
        external_id: Optional[str] = None,
        published_at: Optional[datetime] = None,
        language: str = "en",
    ):
        self.title = title
        self.url = url
        self.content = content
        self.external_id = external_id
        self.published_at = published_at
        self.language = language


class BaseParser(ABC):
    """Abstract base class for all news source parsers."""

    @abstractmethod
    async def parse(self, source: Source) -> list[ParsedArticle]:
        """Fetch and parse articles from the given source.

        Returns a list of ParsedArticle objects.
        Implementations should raise ParserError on fetch/parse failures
        and return an empty list only for valid empty or unchanged sources.
        """
