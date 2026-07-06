from __future__ import annotations

from types import SimpleNamespace
import re

from app.database.models import BotUser, GeneratedArticle, NewsCluster, RawArticle, Source
from app.services import notifier


class _ScalarResult:
    def __init__(self, items: list[object]):
        self._items = items

    def all(self) -> list[object]:
        return self._items


class _FakeSession:
    def __init__(self, *result_sets: list[object]):
        self._result_sets = list(result_sets)

    async def scalars(self, statement: object) -> _ScalarResult:
        return _ScalarResult(self._result_sets.pop(0))


class _FakeBot:
    def __init__(self, effects: list[object]):
        self.effects = effects
        self.sent: list[dict[str, object]] = []

    async def send_message(self, **kwargs: object) -> SimpleNamespace:
        self.sent.append(kwargs)
        effect = self.effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return SimpleNamespace(message_id=effect)


def _article() -> GeneratedArticle:
    return GeneratedArticle(
        id=7,
        cluster_id=3,
        headline="Headline",
        body="Body",
        summary="",
    )


def _cluster() -> NewsCluster:
    return NewsCluster(
        id=3,
        score=50,
        sources_count=1,
        category="general",
        is_hot=False,
    )


def _session(users: list[BotUser]) -> _FakeSession:
    return _FakeSession(
        [RawArticle(id=1, source_id=1, title="Title", url="https://item.test", cluster_id=3)],
        [Source(id=1, name="Source", url="https://source.test", type="rss", weight=5)],
        users,
    )


async def test_send_to_moderators_returns_zero_delivery_result_when_all_sends_fail() -> None:
    article = _article()
    bot = _FakeBot([RuntimeError("blocked"), RuntimeError("forbidden")])
    notifier.set_bot(bot)  # type: ignore[arg-type]
    users = [
        BotUser(id=101, role="admin", is_active=True),
        BotUser(id=202, role="moderator", is_active=True),
    ]

    result = await notifier.send_to_moderators(_session(users), article, _cluster())

    assert result.total_recipients == 2
    assert result.delivered_count == 0
    assert result.failed_count == 2
    assert len(result.errors) == 2
    assert article.telegram_message_id is None
    assert article.telegram_chat_id is None


async def test_send_to_moderators_counts_mixed_delivery_and_stores_first_success() -> None:
    article = _article()
    bot = _FakeBot([RuntimeError("blocked"), 55])
    notifier.set_bot(bot)  # type: ignore[arg-type]
    users = [
        BotUser(id=101, role="admin", is_active=True),
        BotUser(id=202, role="moderator", is_active=True),
    ]

    result = await notifier.send_to_moderators(_session(users), article, _cluster())

    assert result.total_recipients == 2
    assert result.delivered_count == 1
    assert result.failed_count == 1
    assert result.any_delivered is True
    assert article.telegram_message_id == 55
    assert article.telegram_chat_id == 202


async def test_send_to_moderators_routes_to_workspace_group_chat() -> None:
    """Group workspaces get one card into their chat; no DM broadcast query."""
    article = _article()
    bot = _FakeBot([77])
    notifier.set_bot(bot)  # type: ignore[arg-type]
    workspace = SimpleNamespace(id=2, chat_id=-100500)
    # Only two scalar queries (raw articles, sources) — users must not be read.
    session = _FakeSession(
        [RawArticle(id=1, source_id=1, title="Title", url="https://item.test", cluster_id=3)],
        [Source(id=1, name="Source", url="https://source.test", type="rss", weight=5)],
    )

    result = await notifier.send_to_moderators(session, article, _cluster(), workspace)

    assert result.total_recipients == 1
    assert result.delivered_count == 1
    assert bot.sent[0]["chat_id"] == -100500
    assert article.telegram_chat_id == -100500


def test_split_text_keeps_html_entities_and_tags_valid() -> None:
    text = "<b>Headline &amp; title</b>\n\n" + ("Body &amp; details " * 12)

    chunks = notifier._split_text(text, 45)

    assert len(chunks) > 1
    assert all(len(chunk) <= 45 for chunk in chunks)
    assert all(_has_balanced_bold_tags(chunk) for chunk in chunks)
    assert all(not re.search(r"&[^;\s]*$", chunk) for chunk in chunks)
    assert all(not chunk.startswith(("amp;", "lt;", "gt;", "quot;")) for chunk in chunks)


def test_split_text_returns_short_html_unchanged() -> None:
    text = "<b>Short &amp; safe</b>"

    assert notifier._split_text(text, 4096) == [text]


def _has_balanced_bold_tags(text: str) -> bool:
    return text.count("<b>") == text.count("</b>")
