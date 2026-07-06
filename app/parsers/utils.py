"""Shared utilities for parsers: language detection, HTML meta extraction, HTTP headers."""

from __future__ import annotations

import html as html_module
import re
from typing import Optional

# Common browser User-Agent for scraping requests
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def detect_language(text: str) -> str:
    """Simple heuristic: if text has >30% Cyrillic chars, it's Russian."""
    cyrillic_count = sum(1 for ch in text if "\u0400" <= ch <= "\u04ff")
    return "ru" if cyrillic_count > len(text) * 0.3 else "en"


def extract_meta(html: str, property_name: str) -> Optional[str]:
    """Extract content from <meta property='...' content='...'>."""
    for attr in ("property", "name"):
        pattern = (
            rf'<meta\s+[^>]*{attr}=["\']?{re.escape(property_name)}["\']?'
            rf'[^>]*content=["\']([^"\']+)["\']'
        )
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return html_module.unescape(match.group(1).strip())

        # Try reversed order: content before property
        pattern2 = (
            rf'<meta\s+[^>]*content=["\']([^"\']+)["\']'
            rf'[^>]*{attr}=["\']?{re.escape(property_name)}["\']?'
        )
        match2 = re.search(pattern2, html, re.IGNORECASE)
        if match2:
            return html_module.unescape(match2.group(1).strip())

    return None


def extract_html_title(html: str) -> Optional[str]:
    """Extract text from <title>...</title>, strip site name suffix."""
    match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if match:
        title = html_module.unescape(match.group(1).strip())
        for sep in (" | ", " — ", " - ", " – "):
            if sep in title:
                title = title.split(sep)[0].strip()
        return title
    return None
