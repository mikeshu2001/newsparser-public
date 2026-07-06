"""Send generated articles to moderators in Telegram."""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass, field
from typing import Optional

from aiogram import Bot
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    BotUser,
    GeneratedArticle,
    NewsCluster,
    RawArticle,
    Source,
    Workspace,
)
from app.keyboards.moderation import moderation_keyboard

# Module-level bot reference, set by main.py on startup
_bot: Optional[Bot] = None


def set_bot(bot: Bot) -> None:
    """Store bot instance for sending messages."""
    global _bot
    _bot = bot


def get_bot() -> Bot:
    if _bot is None:
        raise RuntimeError("Bot not set — call notifier.set_bot() first")
    return _bot


# Telegram message limit
_TG_LIMIT = 4096
_HTML_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)(?:\s[^>]*)?>")


@dataclass
class DeliveryResult:
    """Telegram moderator notification delivery summary."""

    total_recipients: int
    delivered_count: int = 0
    failed_count: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def any_delivered(self) -> bool:
        return self.delivered_count > 0


async def send_to_moderators(
    session: AsyncSession,
    article: GeneratedArticle,
    cluster: NewsCluster,
    workspace: Workspace | None = None,
) -> DeliveryResult:
    """Send the moderation card.

    Group workspaces get one card into their Telegram chat; the default
    (owner) workspace keeps the legacy DM broadcast to admins/moderators.
    """
    bot = get_bot()

    # Collect source names for the cluster
    raw_articles = (
        await session.scalars(
            select(RawArticle).where(RawArticle.cluster_id == cluster.id)
        )
    ).all()
    source_ids = list({a.source_id for a in raw_articles})
    sources = (
        await session.scalars(select(Source).where(Source.id.in_(source_ids)))
    ).all()
    source_map = {s.id: s.name for s in sources}

    # Build source links: (name, url) from raw articles, one per source
    source_links: list[tuple[str, str | None]] = []
    seen_sources: set[int] = set()
    for ra in raw_articles:
        if ra.source_id not in seen_sources:
            seen_sources.add(ra.source_id)
            source_links.append((source_map.get(ra.source_id, "?"), ra.url))

    # Build message text
    text = _format_message(article, cluster, source_links)

    # Recipients: the workspace group chat, or the legacy DM broadcast
    if workspace is not None and workspace.chat_id:
        chat_ids = [workspace.chat_id]
    else:
        users = (
            await session.scalars(
                select(BotUser).where(
                    BotUser.is_active == True,  # noqa: E712
                    BotUser.role.in_(["admin", "moderator"]),
                )
            )
        ).all()

        if not users:
            logger.warning("No active moderators/admins to notify")
            return DeliveryResult(total_recipients=0)

        chat_ids = [user.id for user in users]

    keyboard = moderation_keyboard(article.id)
    result = DeliveryResult(total_recipients=len(chat_ids))

    # Build once — identical for every recipient.
    is_long = len(text) > _TG_LIMIT
    header = ""
    body_chunks: list[str] = []
    if is_long:
        # Tag-safe shortening: a naive fixed-index truncate could sever an
        # <a> tag or HTML entity and Telegram would reject the message.
        header = _split_text(
            _format_header(article, cluster, source_links), _TG_LIMIT
        )[0]
        body_text = f"<b>{_esc(article.headline)}</b>\n\n{_esc(article.body)}"
        body_chunks = _split_text(body_text, _TG_LIMIT)

    for chat_id in chat_ids:
        try:
            if not is_long:
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=keyboard,
                )
            else:
                # Split: first message = header + buttons, second = full text
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=header,
                    reply_markup=keyboard,
                )
                for chunk in body_chunks:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        reply_to_message_id=msg.message_id,
                    )

            # Save telegram IDs from the first delivery for future editing
            if not article.telegram_message_id:
                article.telegram_message_id = msg.message_id
                article.telegram_chat_id = chat_id

            result.delivered_count += 1
            logger.info(f"Notified chat {chat_id} about article #{article.id}")

        except Exception as e:
            result.failed_count += 1
            result.errors.append(f"{chat_id}: {e}")
            logger.error(f"Failed to notify chat {chat_id}: {e}")

    return result


def _esc(text: str | None) -> str:
    """Escape HTML special chars for Telegram messages."""
    return html_module.escape(text or "")


def _format_sources(source_links: list[tuple[str, str | None]]) -> str:
    """Format source links as clickable Telegram HTML links."""
    if not source_links:
        return "—"
    parts = []
    for name, url in source_links:
        if url:
            parts.append(f'<a href="{_esc(url)}">{_esc(name)}</a>')
        else:
            parts.append(_esc(name))
    return " • ".join(parts)


def _format_message(
    article: GeneratedArticle,
    cluster: NewsCluster,
    source_links: list[tuple[str, str | None]],
) -> str:
    """Format the full moderation message."""
    hot_badge = "🔥 HOT | " if cluster.is_hot else ""
    category = _esc(cluster.category or "general")
    sources_line = _format_sources(source_links)

    summary_line = f"📝 <i>Для соцсетей:</i> {_esc(article.summary)}\n\n" if article.summary else ""
    return (
        f"{hot_badge}Категория: {category}\n\n"
        f"📰 <b>{_esc(article.headline)}</b>\n\n"
        f"{_esc(article.body)}\n\n"
        f"{summary_line}"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 Score: {cluster.score} | Источники: {cluster.sources_count}\n"
        f"🔗 {sources_line}"
    )


def _format_header(
    article: GeneratedArticle,
    cluster: NewsCluster,
    source_links: list[tuple[str, str | None]],
) -> str:
    """Short header for the first message when splitting."""
    hot_badge = "🔥 HOT | " if cluster.is_hot else ""
    category = _esc(cluster.category or "general")
    sources_line = _format_sources(source_links)

    summary_line = f"📝 <i>Для соцсетей:</i> {_esc(article.summary)}\n\n" if article.summary else ""
    return (
        f"{hot_badge}Категория: {category}\n\n"
        f"📰 <b>{_esc(article.headline)}</b>\n\n"
        f"{summary_line}"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 Score: {cluster.score} | Источники: {cluster.sources_count}\n"
        f"🔗 {sources_line}\n\n"
        f"⬇️ Полный текст в ответе ниже"
    )


def _split_text(text: str, limit: int) -> list[str]:
    """Split long Telegram HTML text into valid chunks within the limit."""
    chunks: list[str] = []
    index = 0
    open_tags: list[tuple[str, str, str]] = []

    while index < len(text):
        prefix = "".join(open_tag for _name, open_tag, _close_tag in open_tags)
        content: list[str] = []
        content_len = 0

        while index < len(text):
            token, next_index = _next_html_token(text, index)
            next_open_tags = _apply_html_token(open_tags, token)
            # Arithmetic length check — materializing the candidate string per
            # token would make each chunk O(L^2) and block the event loop.
            candidate_len = (
                len(prefix)
                + content_len
                + len(token)
                + _closing_tags_len(next_open_tags)
            )

            if content and candidate_len > limit:
                break

            content.append(token)
            content_len += len(token)
            open_tags = next_open_tags
            index = next_index

            if candidate_len >= limit:
                break

        if not content:
            token, next_index = _next_html_token(text, index)
            content.append(token[: max(1, limit - len(prefix))])
            index = next_index

        chunk = prefix + "".join(content) + _closing_tags(open_tags)
        chunks.append(chunk)

    return chunks


def _next_html_token(text: str, index: int) -> tuple[str, int]:
    if text[index] == "<":
        end = text.find(">", index + 1)
        if end != -1:
            return text[index:end + 1], end + 1

    if text[index] == "&":
        end = text.find(";", index + 1)
        if end != -1:
            return text[index:end + 1], end + 1

    return text[index], index + 1


def _apply_html_token(
    open_tags: list[tuple[str, str, str]],
    token: str,
) -> list[tuple[str, str, str]]:
    match = _HTML_TAG_RE.fullmatch(token)
    if not match:
        return open_tags

    is_closing, name = match.group(1), match.group(2).lower()
    if is_closing:
        updated = list(open_tags)
        for index in range(len(updated) - 1, -1, -1):
            if updated[index][0] == name:
                del updated[index]
                break
        return updated

    if token.endswith("/>"):
        return open_tags
    return [*open_tags, (name, token, f"</{name}>")]


def _closing_tags(open_tags: list[tuple[str, str, str]]) -> str:
    return "".join(close_tag for _name, _open_tag, close_tag in reversed(open_tags))


def _closing_tags_len(open_tags: list[tuple[str, str, str]]) -> int:
    return sum(len(close_tag) for _name, _open_tag, close_tag in open_tags)
