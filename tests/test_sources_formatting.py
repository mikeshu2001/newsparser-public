from __future__ import annotations

from app.database.models import Source
from app.handlers.sources import (
    _format_existing_source_message,
    _format_source_added_message,
    _format_source_line,
    _format_validation_success,
)
from app.services.parsing_cycle import format_source_error_alert


def test_existing_source_message_escapes_source_name() -> None:
    text = _format_existing_source_message("<b>Bad Feed</b>")

    assert "&lt;b&gt;Bad Feed&lt;/b&gt;" in text
    assert "<b>Bad Feed</b>" not in text


def test_source_added_message_escapes_user_controlled_fields() -> None:
    text = _format_source_added_message(
        name="<script>Name</script>",
        type_str="<RSS>",
        categories="AI <General>",
        weight=7,
    )

    assert "&lt;script&gt;Name&lt;/script&gt;" in text
    assert "&lt;RSS&gt;" in text
    assert "AI &lt;General&gt;" in text
    assert "<script>Name</script>" not in text


def test_source_list_line_escapes_name_and_error() -> None:
    source = Source(
        id=1,
        name="<i>External</i>",
        url="https://example.com/feed.xml",
        type="rss",
        weight=5,
        active=True,
        last_error="<boom>",
    )

    lines = _format_source_line(1, source)
    text = "\n".join(lines)

    assert "&lt;i&gt;External&lt;/i&gt;" in text
    assert "&lt;boom&gt;" in text
    assert "<i>External</i>" not in text


def test_validation_success_escapes_feed_title() -> None:
    text = _format_validation_success("Фид найден! Последняя запись: <b>Title</b>")

    assert "&lt;b&gt;Title&lt;/b&gt;" in text
    assert "<b>Title</b>" not in text


def test_source_error_alert_escapes_source_name_and_error() -> None:
    source = Source(
        id=1,
        name="<b>Feed</b>",
        url="https://example.com/feed.xml",
        type="rss",
        last_error="<timeout>",
    )

    text = format_source_error_alert(source, 5)

    assert "&lt;b&gt;Feed&lt;/b&gt;" in text
    assert "&lt;timeout&gt;" in text
    assert "<b>Feed</b>" not in text
