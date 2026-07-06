"""/add_source FSM dialogue and /sources list management.

Admin-only commands for managing news sources.
"""

from __future__ import annotations

import html as html_module
import math

import feedparser
import httpx
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ForceReply, Message
from loguru import logger
from sqlalchemy import func, select

from app.config import settings as app_settings
from app.database.database import async_session
from app.database.models import DEFAULT_WORKSPACE_ID, BotUser, Source, Workspace
from app.services.workspaces import is_chat_admin, is_group_chat, resolve_workspace
from app.keyboards.sources import (
    ADDABLE_SOURCE_TYPES,
    CATEGORIES,
    PAGE_SIZE,
    SOURCE_TYPES,
    categories_keyboard,
    source_type_keyboard,
    sources_list_keyboard,
    weight_keyboard,
)
from app.states.add_source import AddSourceStates

router = Router()


def _parse_int_param(callback_data: str, index: int = 2) -> int | None:
    """Safely extract an integer parameter from callback data like 'prefix:action:42'."""
    parts = callback_data.split(":")
    if len(parts) <= index:
        return None
    try:
        return int(parts[index])
    except (ValueError, TypeError):
        return None


def _is_admin(bot_user: BotUser) -> bool:
    return bot_user.role == "admin"


async def _deny_admin(callback: CallbackQuery) -> None:
    await callback.answer("⛔ Только для администраторов.", show_alert=True)


async def _deny_admin_message(message: Message) -> None:
    await message.answer("⛔ Только для администраторов.")


async def _manage_workspace_id(message: Message, bot_user: BotUser) -> int | None:
    """Workspace id when the caller may manage sources here, else None (replied).

    Groups: Telegram chat admins of a configured workspace. DM: bot admins
    (the owner's default workspace, no lookup needed).
    """
    chat = getattr(message, "chat", None)
    if is_group_chat(chat):
        async with async_session() as session:
            workspace = await resolve_workspace(session, chat)
        if workspace is None:
            await message.answer("Сначала настройте воркспейс: /setup")
            return None
        if not await is_chat_admin(message.bot, chat.id, message.from_user.id):
            await _deny_admin_message(message)
            return None
        return workspace.id

    if not _is_admin(bot_user):
        await _deny_admin_message(message)
        return None
    return DEFAULT_WORKSPACE_ID


async def _manage_workspace_id_callback(
    callback: CallbackQuery,
    bot_user: BotUser,
) -> int | None:
    message = callback.message
    chat = getattr(message, "chat", None)
    if is_group_chat(chat):
        async with async_session() as session:
            workspace = await resolve_workspace(session, chat)
        if workspace is None:
            await callback.answer("Сначала настройте воркспейс: /setup", show_alert=True)
            return None
        if not await is_chat_admin(callback.bot, chat.id, callback.from_user.id):
            await _deny_admin(callback)
            return None
        return workspace.id

    if not _is_admin(bot_user):
        await _deny_admin(callback)
        return None
    return DEFAULT_WORKSPACE_ID


def _reply_markup_for(message: Message) -> ForceReply | None:
    # Group privacy mode: free-text answers reach the bot only as replies,
    # so prompts in groups request a reply explicitly.
    return ForceReply(selective=True) if is_group_chat(getattr(message, "chat", None)) else None


def _esc(text: object) -> str:
    return html_module.escape(str(text or ""))


def _format_existing_source_message(source_name: str) -> str:
    return f"⚠️ Источник с таким URL уже существует: <b>{_esc(source_name)}</b>"


def _format_source_added_message(
    *,
    name: str,
    type_str: str,
    categories: str,
    weight: int,
) -> str:
    return (
        "✅ Источник добавлен!\n\n"
        f"📌 <b>{_esc(name)}</b> | {_esc(type_str)} | {_esc(categories)} | Вес: {weight}\n"
        f"Начнёт парситься в следующем цикле (~{app_settings.parsing_interval_minutes} мин)."
    )


def _format_source_line(index: int, source: Source) -> list[str]:
    if not source.active:
        status = "🔴"
    elif source.last_error:
        status = "⚠️"
    else:
        status = "🟢"

    type_str = SOURCE_TYPES.get(source.type, source.type)
    lines = [
        f"{index}. {status} <b>{_esc(source.name)}</b> ({_esc(type_str)}, вес {source.weight})"
    ]
    if source.last_error:
        lines.append(f"   <i>Ошибка: {_esc(source.last_error[:80])}</i>")
    return lines


def _format_validation_success(info: str) -> str:
    return f"✅ {_esc(info)}\n\nУкажите имя источника:"


# ---------------------------------------------------------------------------
# /add_source — FSM
# ---------------------------------------------------------------------------

@router.message(Command("add_source"))
async def cmd_add_source(message: Message, bot_user: BotUser, state: FSMContext) -> None:
    workspace_id = await _manage_workspace_id(message, bot_user)
    if workspace_id is None:
        return

    await state.set_state(AddSourceStates.choosing_type)
    await state.update_data(workspace_id=workspace_id)
    await message.answer(
        "Выберите тип источника:",
        reply_markup=source_type_keyboard(),
    )


# Step 1: type chosen
@router.callback_query(AddSourceStates.choosing_type, F.data.startswith("addsrc:type:"))
async def on_type_chosen(
    callback: CallbackQuery,
    state: FSMContext,
    bot_user: BotUser,
) -> None:
    if not is_group_chat(getattr(callback.message, "chat", None)) and not _is_admin(bot_user):
        await state.clear()
        await _deny_admin(callback)
        return

    src_type = callback.data.split(":")[2]
    if src_type not in ADDABLE_SOURCE_TYPES:
        await callback.answer("Неизвестный тип", show_alert=True)
        return

    await state.update_data(source_type=src_type)
    await state.set_state(AddSourceStates.entering_url)

    if src_type == "telegram":
        prompt = "Введите имя Telegram-канала (без @):"
    else:
        prompt = "Введите URL источника:"

    markup = _reply_markup_for(callback.message)
    if markup is not None:
        await callback.message.answer(prompt, reply_markup=markup)
    else:
        await callback.message.edit_text(prompt)
    await callback.answer()


# Step 2: URL entered → validate
@router.message(AddSourceStates.entering_url)
async def on_url_entered(
    message: Message,
    state: FSMContext,
    bot_user: BotUser,
) -> None:
    if not is_group_chat(getattr(message, "chat", None)) and not _is_admin(bot_user):
        await state.clear()
        await _deny_admin_message(message)
        return

    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("Добавление источника отменено. Повторите команду.")
        return

    if not message.text:
        await message.answer("Отправьте URL текстом:", reply_markup=_reply_markup_for(message))
        return

    data = await state.get_data()
    src_type = data["source_type"]
    raw_input = message.text.strip()

    # Build URL for telegram channels
    if src_type == "telegram":
        channel = raw_input.lstrip("@")
        url = f"{app_settings.telegram_rss_service}/@{channel}/feed.xml"
    else:
        url = raw_input

    # Source.url is VARCHAR(500); a longer URL would blow up on INSERT
    # at the very end of the dialogue.
    if len(url) > 500:
        await message.answer(
            "URL слишком длинный (максимум 500 символов). Попробуйте другой URL:"
        )
        return

    await message.answer("⏳ Проверяю источник...")

    # Validate
    ok, info = await _validate_source(src_type, url)
    if not ok:
        await message.answer(
            f"❌ Не удалось загрузить источник.\n{_esc(info)}\n\nПопробуйте другой URL:",
            reply_markup=_reply_markup_for(message),
        )
        return

    await state.update_data(url=url, validation_info=info)
    await state.set_state(AddSourceStates.entering_name)
    await message.answer(
        _format_validation_success(info), reply_markup=_reply_markup_for(message)
    )


# Step 3: name entered
@router.message(AddSourceStates.entering_name)
async def on_name_entered(
    message: Message,
    state: FSMContext,
    bot_user: BotUser,
) -> None:
    if not is_group_chat(getattr(message, "chat", None)) and not _is_admin(bot_user):
        await state.clear()
        await _deny_admin_message(message)
        return

    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("Добавление источника отменено. Повторите команду.")
        return

    if not message.text:
        await message.answer("Отправьте название текстом:", reply_markup=_reply_markup_for(message))
        return

    name = message.text.strip()
    if not name or len(name) > 255:
        await message.answer(
            "Имя должно быть от 1 до 255 символов. Попробуйте ещё:",
            reply_markup=_reply_markup_for(message),
        )
        return

    await state.update_data(name=name, selected_categories=[])
    await state.set_state(AddSourceStates.choosing_categories)
    await message.answer(
        "Выберите категории (можно несколько):",
        reply_markup=categories_keyboard(set()),
    )


# Step 4: category toggle
@router.callback_query(AddSourceStates.choosing_categories, F.data.startswith("addsrc:cat:"))
async def on_category_toggle(
    callback: CallbackQuery,
    state: FSMContext,
    bot_user: BotUser,
) -> None:
    if not is_group_chat(getattr(callback.message, "chat", None)) and not _is_admin(bot_user):
        await state.clear()
        await _deny_admin(callback)
        return

    action = callback.data.split(":")[2]

    if action == "done":
        data = await state.get_data()
        selected = data.get("selected_categories", [])
        if not selected:
            await callback.answer("Выберите хотя бы одну категорию", show_alert=True)
            return

        await state.set_state(AddSourceStates.choosing_weight)
        await callback.message.edit_text(
            "Укажите вес источника — насколько он авторитетный:",
            reply_markup=weight_keyboard(),
        )
        await callback.answer()
        return

    # Toggle category
    data = await state.get_data()
    selected: set[str] = set(data.get("selected_categories", []))
    if action in selected:
        selected.discard(action)
    else:
        selected.add(action)

    await state.update_data(selected_categories=list(selected))
    await callback.message.edit_reply_markup(
        reply_markup=categories_keyboard(selected),
    )
    await callback.answer()


# Step 5: weight chosen → save
@router.callback_query(AddSourceStates.choosing_weight, F.data.startswith("addsrc:weight:"))
async def on_weight_chosen(
    callback: CallbackQuery,
    state: FSMContext,
    bot_user: BotUser,
) -> None:
    if not is_group_chat(getattr(callback.message, "chat", None)) and not _is_admin(bot_user):
        await state.clear()
        await _deny_admin(callback)
        return

    weight = _parse_int_param(callback.data)
    if weight is None:
        await callback.answer("Invalid data", show_alert=True)
        return
    data = await state.get_data()

    src_type = data["source_type"]
    url = data["url"]
    name = data["name"]
    categories = sorted(data.get("selected_categories", []))

    await state.clear()

    workspace_id = data.get("workspace_id", DEFAULT_WORKSPACE_ID)

    async with async_session() as session:
        existing = await session.scalar(
            select(Source).where(
                Source.workspace_id == workspace_id,
                Source.url == url,
            )
        )
        if existing:
            await callback.message.edit_text(_format_existing_source_message(existing.name))
            await callback.answer()
            return

        source = Source(
            workspace_id=workspace_id,
            name=name,
            url=url,
            type=src_type,
            categories=categories,
            weight=weight,
            active=True,
            added_by=callback.from_user.id,
        )
        session.add(source)
        await session.commit()

    cats_str = ", ".join(CATEGORIES.get(c, c) for c in categories)
    type_str = SOURCE_TYPES.get(src_type, src_type)

    await callback.message.edit_text(
        _format_source_added_message(
            name=name,
            type_str=type_str,
            categories=cats_str,
            weight=weight,
        )
    )
    await callback.answer()
    logger.info(f"Source added: {name} ({src_type}) by user {callback.from_user.id}")


# Cancel at any point in add_source FSM
@router.callback_query(F.data == "addsrc:cancel")
async def on_add_source_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    bot_user: BotUser,
) -> None:
    if not is_group_chat(getattr(callback.message, "chat", None)) and not _is_admin(bot_user):
        await state.clear()
        await _deny_admin(callback)
        return

    await state.clear()
    await callback.message.edit_text("Добавление источника отменено.")
    await callback.answer()


# ---------------------------------------------------------------------------
# /sources — paginated list
# ---------------------------------------------------------------------------

@router.message(Command("sources"))
async def cmd_sources(message: Message, bot_user: BotUser) -> None:
    workspace_id = await _manage_workspace_id(message, bot_user)
    if workspace_id is None:
        return

    await _send_sources_page(message, page=1, workspace_id=workspace_id)


@router.callback_query(F.data.startswith("src:page:"))
async def on_sources_page(callback: CallbackQuery, bot_user: BotUser) -> None:
    workspace_id = await _manage_workspace_id_callback(callback, bot_user)
    if workspace_id is None:
        return

    page = _parse_int_param(callback.data)
    if page is None:
        await callback.answer("Invalid data", show_alert=True)
        return
    await _send_sources_page(
        callback.message, page=page, edit=True, workspace_id=workspace_id
    )
    await callback.answer()


@router.callback_query(F.data.startswith("src:toggle:"))
async def on_source_toggle(callback: CallbackQuery, bot_user: BotUser) -> None:
    workspace_id = await _manage_workspace_id_callback(callback, bot_user)
    if workspace_id is None:
        return

    source_id = _parse_int_param(callback.data)
    if source_id is None:
        await callback.answer("Invalid data", show_alert=True)
        return

    async with async_session() as session:
        source = await session.get(Source, source_id)
        if not source or source.workspace_id != workspace_id:
            await callback.answer("Источник не найден", show_alert=True)
            return
        source.active = not source.active
        new_state = "включён" if source.active else "выключен"
        await session.commit()

    await callback.answer(f"{source.name} {new_state}")
    # Refresh current page
    await _send_sources_page(callback.message, page=1, edit=True, workspace_id=workspace_id)


@router.callback_query(F.data.startswith("src:delete:"))
async def on_source_delete(callback: CallbackQuery, bot_user: BotUser) -> None:
    workspace_id = await _manage_workspace_id_callback(callback, bot_user)
    if workspace_id is None:
        return

    source_id = _parse_int_param(callback.data)
    if source_id is None:
        await callback.answer("Invalid data", show_alert=True)
        return

    async with async_session() as session:
        source = await session.get(Source, source_id)
        if not source or source.workspace_id != workspace_id:
            await callback.answer("Источник не найден", show_alert=True)
            return
        name = source.name
        await session.delete(source)
        await session.commit()

    await callback.answer(f"🗑 {name} удалён")
    await _send_sources_page(callback.message, page=1, edit=True, workspace_id=workspace_id)
    logger.info(f"Source deleted: {name} (id={source_id}) by user {callback.from_user.id}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _send_sources_page(
    message: Message,
    page: int = 1,
    edit: bool = False,
    workspace_id: int = DEFAULT_WORKSPACE_ID,
) -> None:
    async with async_session() as session:
        total = await session.scalar(
            select(func.count(Source.id)).where(Source.workspace_id == workspace_id)
        )
        total = total or 0
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        sources = (
            await session.scalars(
                select(Source)
                .where(Source.workspace_id == workspace_id)
                .order_by(Source.id)
                .offset((page - 1) * PAGE_SIZE)
                .limit(PAGE_SIZE)
            )
        ).all()

    if not sources:
        text = "📰 Источников пока нет."
        if edit:
            try:
                await message.edit_text(text)
            except Exception:
                pass
        else:
            await message.answer(text)
        return

    lines = [f"📰 <b>Источники</b> (стр. {page}/{total_pages}):\n"]
    for i, src in enumerate(sources, start=(page - 1) * PAGE_SIZE + 1):
        lines.extend(_format_source_line(i, src))

    text = "\n".join(lines)
    keyboard = sources_list_keyboard(list(sources), page, total_pages)

    if edit:
        try:
            await message.edit_text(text, reply_markup=keyboard)
        except Exception:
            pass  # "message is not modified" — safe to ignore
    else:
        await message.answer(text, reply_markup=keyboard)


async def _validate_source(src_type: str, url: str) -> tuple[bool, str]:
    """Test-fetch a source URL.  Returns (ok, info_message)."""
    timeout = httpx.Timeout(15.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            if src_type in ("rss", "telegram"):
                resp = await client.get(url)
                resp.raise_for_status()
                feed = feedparser.parse(resp.text)
                if not feed.entries:
                    return False, "Фид пустой (0 записей)."
                last_title = feed.entries[0].get("title", "—")
                return True, f"Фид найден! Последняя запись: {last_title}"
            else:
                # web / sitemap — just check HTTP 200
                resp = await client.get(url)
                resp.raise_for_status()
                return True, f"URL доступен (HTTP {resp.status_code})."
    except Exception as e:
        return False, str(e)[:200]
