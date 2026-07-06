"""/approved, /stats and /queue commands — readable by all active roles."""

from __future__ import annotations

import html as html_module
import math
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from app.database.database import async_session
from app.database.models import (
    DEFAULT_WORKSPACE_ID,
    BotUser,
    GeneratedArticle,
    NewsCluster,
    RawArticle,
    Source,
    Workspace,
)
from app.keyboards.common import pagination_keyboard
from app.services.workspaces import is_chat_admin, is_group_chat, resolve_workspace

router = Router()

_PAGE_SIZE = 5


def _parse_int_param(callback_data: str, index: int = 2) -> int | None:
    """Safely extract an integer parameter from callback data like 'prefix:action:42'."""
    parts = callback_data.split(":")
    if len(parts) <= index:
        return None
    try:
        return int(parts[index])
    except (ValueError, TypeError):
        return None


async def _chat_workspace(message: Message) -> Workspace | None:
    """Group chat -> its workspace (with a /setup hint when unset).

    DM returns None: the default workspace id is static, no lookup needed."""
    chat = getattr(message, "chat", None)
    if not is_group_chat(chat):
        return None
    async with async_session() as session:
        workspace = await resolve_workspace(session, chat)
    if workspace is None:
        await message.answer("Воркспейс не настроен. Запустите /setup")
    return workspace


def _workspace_id_of(workspace: Workspace | None) -> int:
    return workspace.id if workspace is not None else DEFAULT_WORKSPACE_ID


async def _can_clear_here(
    callback: CallbackQuery,
    bot_user: BotUser,
    *,
    page_wide: bool,
) -> bool:
    """Groups: Telegram chat admins. DM: the existing bot-role rules."""
    chat = getattr(callback.message, "chat", None)
    if is_group_chat(chat):
        return await is_chat_admin(callback.bot, chat.id, callback.from_user.id)
    return _can_clear_page(bot_user) if page_wide else _can_clear_one(bot_user)


# ---------------------------------------------------------------------------
# /approved — paginated list of approved articles (last 7 days)
# ---------------------------------------------------------------------------

@router.message(Command("approved"))
async def cmd_approved(message: Message, bot_user: BotUser) -> None:
    workspace = await _chat_workspace(message)
    if workspace is None and is_group_chat(getattr(message, "chat", None)):
        return
    await _send_approved_page(message, page=1, workspace=workspace)


@router.callback_query(F.data.startswith("appr:page:"))
async def on_approved_page(callback: CallbackQuery) -> None:
    page = _parse_int_param(callback.data)
    if page is None:
        await callback.answer("Invalid data", show_alert=True)
        return
    workspace = await _chat_workspace(callback.message)
    await _send_approved_page(callback.message, page=page, edit=True, workspace=workspace)
    await callback.answer()


@router.callback_query(F.data.startswith("appr:text:"))
async def on_get_text(callback: CallbackQuery) -> None:
    """Send full article text as a separate message (copy-paste friendly)."""
    article_id = _parse_int_param(callback.data)
    if article_id is None:
        await callback.answer("Invalid data", show_alert=True)
        return

    workspace = await _chat_workspace(callback.message)
    async with async_session() as session:
        article = await session.scalar(
            select(GeneratedArticle)
            .join(NewsCluster, GeneratedArticle.cluster_id == NewsCluster.id)
            .where(
                GeneratedArticle.id == article_id,
                GeneratedArticle.status == "approved",
                NewsCluster.workspace_id == _workspace_id_of(workspace),
            )
        )

    if not article:
        await callback.answer("Статья не найдена", show_alert=True)
        return

    text = f"{article.headline}\n\n{article.body}"
    if article.summary:
        text += f"\n\n---\nДля соцсетей: {article.summary}"

    # Send as plain text (no HTML parsing) for easy copy-paste
    if len(text) <= 4096:
        await callback.message.answer(text, parse_mode=None)
    else:
        for i in range(0, len(text), 4096):
            await callback.message.answer(text[i:i + 4096], parse_mode=None)

    await callback.answer()


async def _send_approved_page(
    message: Message,
    page: int = 1,
    edit: bool = False,
    workspace: Workspace | None = None,
) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    workspace_id = _workspace_id_of(workspace)

    async with async_session() as session:
        # Count
        total = await session.scalar(
            select(func.count(GeneratedArticle.id))
            .join(NewsCluster, GeneratedArticle.cluster_id == NewsCluster.id)
            .where(
                GeneratedArticle.status == "approved",
                GeneratedArticle.created_at >= since,
                NewsCluster.workspace_id == workspace_id,
            )
        )
        total = total or 0
        total_pages = max(1, math.ceil(total / _PAGE_SIZE))
        page = max(1, min(page, total_pages))

        # Fetch page
        articles = (
            await session.scalars(
                select(GeneratedArticle)
                .join(NewsCluster, GeneratedArticle.cluster_id == NewsCluster.id)
                .where(
                    GeneratedArticle.status == "approved",
                    GeneratedArticle.created_at >= since,
                    NewsCluster.workspace_id == workspace_id,
                )
                .order_by(GeneratedArticle.created_at.desc())
                .offset((page - 1) * _PAGE_SIZE)
                .limit(_PAGE_SIZE)
            )
        ).all()

        # Resolve moderator usernames
        mod_ids = [a.moderated_by for a in articles if a.moderated_by]
        mod_map: dict[int, str] = {}
        if mod_ids:
            mods = (
                await session.scalars(
                    select(BotUser).where(BotUser.id.in_(mod_ids))
                )
            ).all()
            mod_map = {
                m.id: f"@{m.username}" if m.username else str(m.id)
                for m in mods
            }

    if not articles:
        text = "📋 Нет одобренных статей за последние 7 дней."
        if edit:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return

    lines = [f"📋 <b>Одобренные статьи</b> (стр. {page}/{total_pages}):\n"]
    rows: list[list[InlineKeyboardButton]] = []

    for i, art in enumerate(articles, start=(page - 1) * _PAGE_SIZE + 1):
        created = art.created_at.strftime("%d.%m.%Y %H:%M") if art.created_at else "—"
        mod_name = mod_map.get(art.moderated_by, "—") if art.moderated_by else "—"
        lines.append(
            f"{i}. 📰 {html_module.escape(art.headline)}\n"
            f"   ✅ Одобрена {created} ({html_module.escape(mod_name)})"
        )
        rows.append([
            InlineKeyboardButton(
                text=f"📄 Получить текст #{i}",
                callback_data=f"appr:text:{art.id}",
            )
        ])

    if total_pages > 1:
        rows.append(pagination_keyboard("appr", page, total_pages))

    text = "\n".join(lines)
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)

    if edit:
        try:
            await message.edit_text(text, reply_markup=keyboard)
        except Exception:
            pass
    else:
        await message.answer(text, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------

@router.message(Command("stats"))
async def cmd_stats(message: Message, bot_user: BotUser) -> None:
    workspace = await _chat_workspace(message)
    if workspace is None and is_group_chat(getattr(message, "chat", None)):
        return
    workspace_id = _workspace_id_of(workspace)
    since = datetime.now(timezone.utc) - timedelta(days=7)

    async with async_session() as session:
        raw_count = await session.scalar(
            select(func.count(RawArticle.id))
            .join(Source, RawArticle.source_id == Source.id)
            .where(
                RawArticle.fetched_at >= since,
                Source.workspace_id == workspace_id,
            )
        ) or 0

        cluster_count = await session.scalar(
            select(func.count(NewsCluster.id)).where(
                NewsCluster.created_at >= since,
                NewsCluster.workspace_id == workspace_id,
            )
        ) or 0

        passed_threshold = await session.scalar(
            select(func.count(NewsCluster.id)).where(
                NewsCluster.created_at >= since,
                NewsCluster.workspace_id == workspace_id,
                NewsCluster.status.notin_(["new"]),
            )
        ) or 0

        generated = await session.scalar(
            select(func.count(GeneratedArticle.id))
            .join(NewsCluster, GeneratedArticle.cluster_id == NewsCluster.id)
            .where(
                GeneratedArticle.created_at >= since,
                NewsCluster.workspace_id == workspace_id,
            )
        ) or 0

        approved = await session.scalar(
            select(func.count(GeneratedArticle.id))
            .join(NewsCluster, GeneratedArticle.cluster_id == NewsCluster.id)
            .where(
                GeneratedArticle.created_at >= since,
                GeneratedArticle.status == "approved",
                NewsCluster.workspace_id == workspace_id,
            )
        ) or 0

        rejected = await session.scalar(
            select(func.count(GeneratedArticle.id))
            .join(NewsCluster, GeneratedArticle.cluster_id == NewsCluster.id)
            .where(
                GeneratedArticle.created_at >= since,
                GeneratedArticle.status == "rejected",
                NewsCluster.workspace_id == workspace_id,
            )
        ) or 0

        pending = await session.scalar(
            select(func.count(NewsCluster.id)).where(
                NewsCluster.created_at >= since,
                NewsCluster.workspace_id == workspace_id,
                NewsCluster.status == "pending_review",
            )
        ) or 0

        hot_count = await session.scalar(
            select(func.count(NewsCluster.id)).where(
                NewsCluster.created_at >= since,
                NewsCluster.workspace_id == workspace_id,
                NewsCluster.is_hot == True,  # noqa: E712
            )
        ) or 0

        avg_score_row = await session.scalar(
            select(func.avg(NewsCluster.score)).where(
                NewsCluster.created_at >= since,
                NewsCluster.workspace_id == workspace_id,
                NewsCluster.status == "approved",
            )
        )
        avg_score = round(avg_score_row) if avg_score_row else 0

    text = (
        "📊 <b>Статистика за последние 7 дней:</b>\n\n"
        f"Собрано новостей: {raw_count}\n"
        f"Уникальных тем (кластеров): {cluster_count}\n"
        f"Прошли порог: {passed_threshold}\n"
        f"Сгенерировано статей: {generated}\n"
        f"Одобрено: {approved}\n"
        f"Отклонено: {rejected}\n"
        f"Ожидают модерации: {pending}\n\n"
        f"🔥 HOT новостей: {hot_count}\n"
        f"📈 Средний score одобренных: {avg_score}"
    )
    await message.answer(text)


# ---------------------------------------------------------------------------
# /queue — clusters awaiting generation or moderation
# ---------------------------------------------------------------------------

_QUEUE_PAGE_SIZE = 10
_VISIBLE_QUEUE_STATUSES = ["new", "waiting", "generating", "pending_review"]
_CLEARABLE_QUEUE_STATUSES = ["new", "waiting"]

_STATUS_EMOJI = {
    "new": "🆕",
    "waiting": "⏳",
    "generating": "⚙️",
    "pending_review": "🔔",
}


def _can_clear_one(bot_user: BotUser) -> bool:
    return bot_user.role in {"admin", "moderator"}


def _can_clear_page(bot_user: BotUser) -> bool:
    return bot_user.role == "admin"


def _format_age(dt: datetime) -> str:
    """Return human-readable age string for a datetime."""
    delta = datetime.now(timezone.utc) - dt
    minutes = int(delta.total_seconds() / 60)
    if minutes < 60:
        return f"{minutes}мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}ч назад"
    days = hours // 24
    return f"{days}д назад"


@router.message(Command("queue"))
async def cmd_queue(message: Message, bot_user: BotUser) -> None:
    workspace = await _chat_workspace(message)
    if workspace is None and is_group_chat(getattr(message, "chat", None)):
        return
    await _send_queue_page(message, page=1, bot_user=bot_user, workspace=workspace)


@router.callback_query(F.data.startswith("queue:clear:"))
async def on_queue_clear(callback: CallbackQuery, bot_user: BotUser) -> None:
    """Reject a cluster directly from the queue view."""
    if not await _can_clear_here(callback, bot_user, page_wide=False):
        await callback.answer("Недостаточно прав для очистки очереди", show_alert=True)
        return
    workspace = await _chat_workspace(callback.message)

    parts = callback.data.split(":")
    try:
        cluster_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 1
    except (IndexError, ValueError):
        await callback.answer("Invalid data", show_alert=True)
        return

    async with async_session() as session:
        cluster = await session.get(NewsCluster, cluster_id)
        if not cluster or (cluster.workspace_id or DEFAULT_WORKSPACE_ID) != _workspace_id_of(workspace):
            await callback.answer("Кластер не найден", show_alert=True)
            return
        if cluster.status in ("approved", "rejected"):
            await callback.answer("Кластер уже обработан", show_alert=True)
            return
        if cluster.status not in _CLEARABLE_QUEUE_STATUSES:
            await callback.answer("Кластер уже на модерации", show_alert=True)
            return
        cluster.status = "rejected"
        cluster.status_changed_at = datetime.now(timezone.utc)
        await session.commit()

    await callback.answer("Кластер удалён из очереди")
    await _send_queue_page(
        callback.message, page=page, edit=True, bot_user=bot_user, workspace=workspace
    )


@router.callback_query(F.data.startswith("queue:clear_all:"))
async def on_queue_clear_all(callback: CallbackQuery, bot_user: BotUser) -> None:
    """Reject all clusters on the current page."""
    if not await _can_clear_here(callback, bot_user, page_wide=True):
        await callback.answer("Недостаточно прав для очистки страницы", show_alert=True)
        return
    workspace = await _chat_workspace(callback.message)

    page = _parse_int_param(callback.data)
    if page is None:
        await callback.answer("Invalid data", show_alert=True)
        return

    async with async_session() as session:
        # Re-derive the page exactly as it was rendered (visible statuses):
        # offsetting into a clearable-only list would reject clusters the
        # admin never saw on this page.
        clusters = (
            await session.scalars(
                select(NewsCluster)
                .where(
                    NewsCluster.workspace_id == _workspace_id_of(workspace),
                    NewsCluster.status.in_(_VISIBLE_QUEUE_STATUSES),
                )
                .order_by(NewsCluster.score.desc(), NewsCluster.first_seen_at.desc())
                .offset((page - 1) * _QUEUE_PAGE_SIZE)
                .limit(_QUEUE_PAGE_SIZE)
            )
        ).all()

        now = datetime.now(timezone.utc)
        count = 0
        for cl in clusters:
            if cl.status not in _CLEARABLE_QUEUE_STATUSES:
                continue
            cl.status = "rejected"
            cl.status_changed_at = now
            count += 1
        await session.commit()

    await callback.answer(f"Очищено {count} кластеров")
    await _send_queue_page(
        callback.message, page=page, edit=True, bot_user=bot_user, workspace=workspace
    )


@router.callback_query(F.data.startswith("queue:page:"))
async def on_queue_page(callback: CallbackQuery, bot_user: BotUser) -> None:
    page = _parse_int_param(callback.data)
    if page is None:
        await callback.answer("Invalid data", show_alert=True)
        return
    workspace = await _chat_workspace(callback.message)
    await _send_queue_page(
        callback.message, page=page, edit=True, bot_user=bot_user, workspace=workspace
    )
    await callback.answer()


def _build_queue_rows(
    clusters: list[NewsCluster],
    page: int,
    total_pages: int,
    can_clear_one: bool,
    can_clear_page: bool,
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []

    if can_clear_one:
        for i, cl in enumerate(clusters, start=(page - 1) * _QUEUE_PAGE_SIZE + 1):
            if cl.status not in _CLEARABLE_QUEUE_STATUSES:
                continue
            rows.append([
                InlineKeyboardButton(
                    text=f"🗑 Очистить #{i}",
                    callback_data=f"queue:clear:{cl.id}:{page}",
                )
            ])

    if (
        any(cl.status in _CLEARABLE_QUEUE_STATUSES for cl in clusters)
        and can_clear_page
    ):
        rows.append([
            InlineKeyboardButton(
                text="🗑 Очистить все на странице",
                callback_data=f"queue:clear_all:{page}",
            )
        ])

    if total_pages > 1:
        rows.append(pagination_keyboard("queue", page, total_pages))

    return rows


async def _send_queue_page(
    message: Message,
    page: int = 1,
    edit: bool = False,
    bot_user: BotUser | None = None,
    workspace: Workspace | None = None,
) -> None:
    workspace_id = _workspace_id_of(workspace)
    async with async_session() as session:
        total = await session.scalar(
            select(func.count(NewsCluster.id)).where(
                NewsCluster.workspace_id == workspace_id,
                NewsCluster.status.in_(_VISIBLE_QUEUE_STATUSES),
            )
        ) or 0
        total_pages = max(1, math.ceil(total / _QUEUE_PAGE_SIZE))
        page = max(1, min(page, total_pages))

        clusters = (
            await session.scalars(
                select(NewsCluster)
                .where(
                    NewsCluster.workspace_id == workspace_id,
                    NewsCluster.status.in_(_VISIBLE_QUEUE_STATUSES),
                )
                .order_by(NewsCluster.score.desc(), NewsCluster.first_seen_at.desc())
                .offset((page - 1) * _QUEUE_PAGE_SIZE)
                .limit(_QUEUE_PAGE_SIZE)
            )
        ).all()

    if not clusters:
        text = "📋 Очередь пуста. Нет кластеров, ожидающих обработки."
        if edit:
            try:
                await message.edit_text(text)
            except Exception:
                pass  # "message is not modified" — safe to ignore
        else:
            await message.answer(text)
        return

    lines = [f"📋 <b>Очередь кластеров</b> (стр. {page}/{total_pages}):\n"]
    for i, cl in enumerate(clusters, start=(page - 1) * _QUEUE_PAGE_SIZE + 1):
        emoji = _STATUS_EMOJI.get(cl.status, "❓")
        hot = " 🔥" if cl.is_hot else ""
        title = html_module.escape(cl.topic_original or cl.topic or "Без названия")
        age = _format_age(cl.first_seen_at) if cl.first_seen_at else "—"
        sources = cl.sources_count or 1
        lines.append(
            f"{i}. {emoji}{hot} {title}\n"
            f"   Score: {cl.score} | {sources} источн. | {age}"
        )

    text = "\n".join(lines)
    if is_group_chat(getattr(message, "chat", None)):
        # Buttons render once for the whole group; presses are guarded
        # server-side by the chat-admin check.
        rows = _build_queue_rows(list(clusters), page, total_pages, True, True)
    elif bot_user is None:
        rows = []
    else:
        rows = _build_queue_rows(
            list(clusters),
            page,
            total_pages,
            _can_clear_one(bot_user),
            _can_clear_page(bot_user),
        )
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    if edit:
        try:
            await message.edit_text(text, reply_markup=keyboard)
        except Exception:
            pass
    else:
        await message.answer(text, reply_markup=keyboard)
