"""Callback handlers for article moderation buttons.

Handles: mod:approve, mod:reject, mod:edit, mod:regen callbacks.
"""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ForceReply, Message
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import async_session
from app.database.models import (
    DEFAULT_WORKSPACE_ID,
    BotUser,
    GeneratedArticle,
    NewsCluster,
    Workspace,
)
from app.services.workspaces import is_group_chat
from app.keyboards.moderation import moderation_keyboard
from app.services.content_generator import generate_article
from app.services.generation_pipeline import mark_manual_generation, unmark_manual_generation
from app.services.notifier import send_to_moderators
from app.states.moderation import ModerationStates

router = Router()
_STALE_ARTICLE_MESSAGE = "Эта версия статьи устарела. Откройте последнюю карточку."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_article_and_cluster(
    session: AsyncSession,
    article_id: int,
    *,
    lock: bool = False,
) -> tuple[GeneratedArticle | None, NewsCluster | None]:
    """Load article and its cluster; return (None, None) if not found.

    If *lock* is True, acquires a FOR UPDATE lock on the cluster row
    to prevent concurrent moderation race conditions.
    """
    article = await session.get(GeneratedArticle, article_id)
    if not article:
        return None, None
    cluster = await session.get(
        NewsCluster, article.cluster_id, with_for_update=lock,
    )
    return article, cluster


async def _is_current_draft_article(
    session: AsyncSession,
    article: GeneratedArticle,
) -> bool:
    if article.status != "draft":
        return False

    latest_version = await session.scalar(
        select(func.max(GeneratedArticle.version)).where(
            GeneratedArticle.cluster_id == article.cluster_id
        )
    )
    return article.version == latest_version


def _parse_article_id(callback_data: str) -> int | None:
    """Extract article_id from callback data like 'mod:action:42'."""
    parts = callback_data.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


def _can_moderate(bot_user: BotUser) -> bool:
    return bot_user.role in {"admin", "moderator"}


def _mark_generating(cluster: NewsCluster) -> None:
    now = datetime.now(timezone.utc)
    cluster.status = "generating"
    cluster.updated_at = now
    cluster.status_changed_at = now


async def _deny_moderation(callback: CallbackQuery) -> None:
    await callback.answer("Недостаточно прав для модерации", show_alert=True)


async def _moderation_allowed(
    callback: CallbackQuery,
    session: object,
    cluster: NewsCluster,
    bot_user: BotUser,
) -> bool:
    """Default workspace: bot roles. Group workspaces: the card must be
    pressed inside its own group chat (membership is the permission)."""
    workspace_id = getattr(cluster, "workspace_id", None) or DEFAULT_WORKSPACE_ID
    if workspace_id == DEFAULT_WORKSPACE_ID:
        if not _can_moderate(bot_user):
            await _deny_moderation(callback)
            return False
        return True

    workspace = await session.get(Workspace, workspace_id)
    chat = getattr(callback.message, "chat", None)
    if workspace is None or chat is None or chat.id != workspace.chat_id:
        await _safe_answer_callback(
            callback, "Эта карточка принадлежит другому воркспейсу.", show_alert=True
        )
        return False
    return True


async def _moderation_allowed_message(
    message: Message,
    session: object,
    cluster: NewsCluster,
    bot_user: BotUser,
) -> bool:
    workspace_id = getattr(cluster, "workspace_id", None) or DEFAULT_WORKSPACE_ID
    if workspace_id == DEFAULT_WORKSPACE_ID:
        if not _can_moderate(bot_user):
            await message.answer("Недостаточно прав для модерации")
            return False
        return True

    workspace = await session.get(Workspace, workspace_id)
    if workspace is None or message.chat.id != workspace.chat_id:
        await message.answer("Эта статья принадлежит другому воркспейсу.")
        return False
    return True


async def _safe_edit_callback_message(
    callback: CallbackQuery,
    text: str,
    **kwargs: object,
) -> None:
    try:
        await callback.message.edit_text(text, **kwargs)
    except Exception as e:
        logger.warning(f"Failed to edit moderation callback message: {e}")


async def _safe_answer_callback(
    callback: CallbackQuery,
    text: str | None = None,
    *,
    show_alert: bool = False,
) -> None:
    try:
        await callback.answer(text, show_alert=show_alert)
    except Exception as e:
        logger.warning(f"Failed to answer moderation callback: {e}")


def _callback_message_html(callback: CallbackQuery) -> str | None:
    """html_text of the callback's card; None for old/inaccessible messages.

    Cards older than ~48h arrive as InaccessibleMessage, which has no
    html_text — touching it directly would crash the handler.
    """
    try:
        return getattr(callback.message, "html_text", None)
    except Exception:
        return None


async def _notify_moderator(callback: CallbackQuery, text: str) -> None:
    """Reply in the card's chat, falling back to a direct message."""
    message = callback.message
    if message is not None and hasattr(message, "answer"):
        try:
            await message.answer(text)
            return
        except Exception as e:
            logger.warning(f"Failed to reply in moderation chat: {e}")
    try:
        await callback.bot.send_message(chat_id=callback.from_user.id, text=text)
    except Exception as e:
        logger.warning(f"Failed to notify moderator {callback.from_user.id}: {e}")


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("mod:approve:"))
async def on_approve(callback: CallbackQuery, bot_user: BotUser) -> None:
    # DM cards always belong to the default workspace — deny non-moderators
    # before touching the DB. Group cards are checked against their workspace
    # after the cluster is loaded.
    if not is_group_chat(getattr(callback.message, "chat", None)) and not _can_moderate(bot_user):
        await _deny_moderation(callback)
        return

    article_id = _parse_article_id(callback.data)
    if article_id is None:
        await callback.answer("Invalid data", show_alert=True)
        return

    async with async_session() as session:
        article, cluster = await _get_article_and_cluster(
            session, article_id, lock=True,
        )

        if not article or not cluster:
            await callback.answer("Статья не найдена", show_alert=True)
            return
        if not await _moderation_allowed(callback, session, cluster, bot_user):
            return

        if cluster.status != "pending_review":
            await callback.answer(
                "Эта статья уже обработана другим модератором.",
                show_alert=True,
            )
            return
        if not await _is_current_draft_article(session, article):
            await callback.answer(_STALE_ARTICLE_MESSAGE, show_alert=True)
            return

        cluster.status = "approved"
        cluster.status_changed_at = datetime.now(timezone.utc)
        article.status = "approved"
        article.moderated_by = callback.from_user.id

        await session.commit()

    logger.info(
        f"Article #{article_id} approved by user {callback.from_user.id}"
    )

    approved_note = "✅ Статья одобрена! Верстальщик может забрать через /approved."
    original_html = _callback_message_html(callback)
    await _safe_edit_callback_message(
        callback,
        f"{original_html}\n\n{approved_note}" if original_html else approved_note,
        reply_markup=None,
    )
    await _safe_answer_callback(callback, "Одобрено!")


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("mod:reject:"))
async def on_reject(callback: CallbackQuery, bot_user: BotUser) -> None:
    # DM cards always belong to the default workspace — deny non-moderators
    # before touching the DB. Group cards are checked against their workspace
    # after the cluster is loaded.
    if not is_group_chat(getattr(callback.message, "chat", None)) and not _can_moderate(bot_user):
        await _deny_moderation(callback)
        return

    article_id = _parse_article_id(callback.data)
    if article_id is None:
        await callback.answer("Invalid data", show_alert=True)
        return

    async with async_session() as session:
        article, cluster = await _get_article_and_cluster(
            session, article_id, lock=True,
        )

        if not article or not cluster:
            await callback.answer("Статья не найдена", show_alert=True)
            return
        if not await _moderation_allowed(callback, session, cluster, bot_user):
            return

        if cluster.status != "pending_review":
            await callback.answer(
                "Эта статья уже обработана другим модератором.",
                show_alert=True,
            )
            return
        if not await _is_current_draft_article(session, article):
            await callback.answer(_STALE_ARTICLE_MESSAGE, show_alert=True)
            return

        cluster.status = "rejected"
        cluster.status_changed_at = datetime.now(timezone.utc)
        article.status = "rejected"
        article.moderated_by = callback.from_user.id

        await session.commit()

    logger.info(
        f"Article #{article_id} rejected by user {callback.from_user.id}"
    )

    rejected_note = "❌ Статья отклонена."
    original_html = _callback_message_html(callback)
    await _safe_edit_callback_message(
        callback,
        f"{original_html}\n\n{rejected_note}" if original_html else rejected_note,
        reply_markup=None,
    )
    await _safe_answer_callback(callback, "Отклонено")


# ---------------------------------------------------------------------------
# Regenerate
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("mod:regen:"))
async def on_regen(callback: CallbackQuery, bot_user: BotUser) -> None:
    # DM cards always belong to the default workspace — deny non-moderators
    # before touching the DB. Group cards are checked against their workspace
    # after the cluster is loaded.
    if not is_group_chat(getattr(callback.message, "chat", None)) and not _can_moderate(bot_user):
        await _deny_moderation(callback)
        return

    article_id = _parse_article_id(callback.data)
    if article_id is None:
        await callback.answer("Invalid data", show_alert=True)
        return

    async with async_session() as session:
        article, cluster = await _get_article_and_cluster(
            session, article_id, lock=True,
        )

        if not article or not cluster:
            await callback.answer("Статья не найдена", show_alert=True)
            return
        if not await _moderation_allowed(callback, session, cluster, bot_user):
            return

        if cluster.status != "pending_review":
            await callback.answer(
                "Эта статья уже обработана другим модератором.",
                show_alert=True,
            )
            return
        if not await _is_current_draft_article(session, article):
            await callback.answer(_STALE_ARTICLE_MESSAGE, show_alert=True)
            return

        original_card_html = _callback_message_html(callback)
        cluster_id = cluster.id
        mark_manual_generation(cluster_id)
        try:
            # Move cluster back to generating for re-generation.
            _mark_generating(cluster)
            await session.commit()

            await _safe_edit_callback_message(
                callback,
                "🔄 Перегенерация...",
                reply_markup=None,
            )
            await _safe_answer_callback(callback, "Генерируем новую версию...")

            # Generate new version
            try:
                new_article = await generate_article(session, cluster)
            except Exception as e:
                logger.error(f"Regen failed for cluster #{cluster_id}: {e}")
                # A failed flush leaves the session aborted; without rollback
                # the recovery commit below raises PendingRollbackError.
                await session.rollback()
                new_article = None

            if new_article:
                # Commit generated article before sending notifications —
                # if send_to_moderators fails, the article is still persisted.
                await session.commit()
                workspace = (
                    await session.get(Workspace, cluster.workspace_id)
                    if cluster.workspace_id
                    else None
                )
                delivery = await send_to_moderators(
                    session, new_article, cluster, workspace
                )
                await session.commit()
                if not delivery.any_delivered:
                    logger.error(
                        f"Regenerated article #{new_article.id} for cluster #{cluster.id} "
                        "was not delivered to any moderator/admin"
                    )
                logger.info(
                    f"Regenerated article for cluster #{cluster.id} "
                    f"→ article #{new_article.id} v{new_article.version}"
                )
            else:
                # Revert to pending_review so moderator can retry. Re-fetch:
                # the rollback above expires instances loaded in this session.
                cluster = await session.get(NewsCluster, cluster_id)
                if cluster is not None:
                    cluster.status = "pending_review"
                    cluster.status_changed_at = datetime.now(timezone.utc)
                    await session.commit()
                if original_card_html:
                    await _safe_edit_callback_message(
                        callback,
                        original_card_html,
                        reply_markup=moderation_keyboard(article.id),
                    )
                await _notify_moderator(
                    callback, "Не удалось перегенерировать. Попробуйте позже."
                )
        finally:
            unmark_manual_generation(cluster_id)


# ---------------------------------------------------------------------------
# Edit (FSM: ask for comment → regenerate with comment)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("mod:edit:"))
async def on_edit(
    callback: CallbackQuery,
    state: FSMContext,
    bot_user: BotUser,
) -> None:
    # DM cards always belong to the default workspace — deny non-moderators
    # before touching the DB. Group cards are checked against their workspace
    # after the cluster is loaded.
    if not is_group_chat(getattr(callback.message, "chat", None)) and not _can_moderate(bot_user):
        await _deny_moderation(callback)
        return

    article_id = _parse_article_id(callback.data)
    if article_id is None:
        await callback.answer("Invalid data", show_alert=True)
        return

    async with async_session() as session:
        article, cluster = await _get_article_and_cluster(
            session, article_id, lock=True,
        )

        if not article or not cluster:
            await callback.answer("Статья не найдена", show_alert=True)
            return
        if not await _moderation_allowed(callback, session, cluster, bot_user):
            return

        if cluster.status != "pending_review":
            await callback.answer(
                "Эта статья уже обработана другим модератором.",
                show_alert=True,
            )
            return
        if not await _is_current_draft_article(session, article):
            await callback.answer(_STALE_ARTICLE_MESSAGE, show_alert=True)
            return

    # Enter FSM — save article_id, ask for edit instructions
    await state.set_state(ModerationStates.waiting_for_edit_comment)
    await state.update_data(article_id=article_id)

    await callback.message.answer(
        "✏️ Напишите, что нужно исправить:\n\n"
        "<i>Например: «Убери последний абзац, добавь про цену»</i>",
        reply_markup=(
            ForceReply(selective=True)
            if is_group_chat(callback.message.chat)
            else None
        ),
    )
    await callback.answer()


@router.message(ModerationStates.waiting_for_edit_comment)
async def on_edit_comment(
    message: Message,
    state: FSMContext,
    bot_user: BotUser,
) -> None:
    if not is_group_chat(getattr(message, "chat", None)) and not _can_moderate(bot_user):
        await state.clear()
        await message.answer("Недостаточно прав для модерации")
        return

    # Don't capture bot commands — cancel FSM and let the command be re-sent
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("Редактирование отменено. Повторите команду.")
        return

    data = await state.get_data()
    article_id = data.get("article_id")

    if not article_id:
        await state.clear()
        await message.answer("Ошибка: не найден article_id. Попробуйте заново.")
        return

    edit_comment = message.text
    if not edit_comment:
        await message.answer("Пустой комментарий. Напишите, что нужно исправить:")
        return

    await state.clear()
    await message.answer("🔄 Перегенерация с учётом правок...")

    async with async_session() as session:
        article, cluster = await _get_article_and_cluster(
            session, article_id, lock=True,
        )

        if not article or not cluster:
            await message.answer("Статья не найдена.")
            return
        if not await _moderation_allowed_message(message, session, cluster, bot_user):
            return

        if cluster.status != "pending_review":
            await message.answer(
                "Эта статья уже обработана другим модератором."
            )
            return
        if not await _is_current_draft_article(session, article):
            await message.answer(_STALE_ARTICLE_MESSAGE)
            return

        cluster_id = cluster.id
        mark_manual_generation(cluster_id)
        try:
            _mark_generating(cluster)
            await session.commit()

            try:
                new_article = await generate_article(
                    session, cluster, edit_comment=edit_comment,
                )
            except Exception as e:
                logger.error(f"Edit-regen failed for cluster #{cluster_id}: {e}")
                # A failed flush leaves the session aborted; without rollback
                # the recovery commit below raises PendingRollbackError.
                await session.rollback()
                new_article = None

            if new_article:
                # Commit generated article before sending notifications —
                # if send_to_moderators fails, the article is still persisted.
                await session.commit()
                workspace = (
                    await session.get(Workspace, cluster.workspace_id)
                    if cluster.workspace_id
                    else None
                )
                delivery = await send_to_moderators(
                    session, new_article, cluster, workspace
                )
                await session.commit()
                if not delivery.any_delivered:
                    logger.error(
                        f"Edited article #{new_article.id} for cluster #{cluster.id} "
                        "was not delivered to any moderator/admin"
                    )
                logger.info(
                    f"Edited article for cluster #{cluster.id} "
                    f"→ article #{new_article.id} v{new_article.version} "
                    f"comment: {edit_comment!r}"
                )
            else:
                # Re-fetch: the rollback above expires loaded instances.
                cluster = await session.get(NewsCluster, cluster_id)
                if cluster is not None:
                    cluster.status = "pending_review"
                    cluster.status_changed_at = datetime.now(timezone.utc)
                    await session.commit()
                await message.answer(
                    "Не удалось перегенерировать. Попробуйте позже."
                )
        finally:
            unmark_manual_generation(cluster_id)
