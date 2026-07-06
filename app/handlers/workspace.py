"""Workspace lifecycle: /setup in a group, topic/keywords, BYOK key in DM."""

from __future__ import annotations

import html as html_module

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger
from sqlalchemy import select

from app.database.database import async_session
from app.database.models import BotUser, Workspace
from app.services.workspaces import is_chat_admin, is_group_chat, resolve_workspace

router = Router()


class ConnectKeyStates(StatesGroup):
    waiting_for_key = State()


def _esc(text: object) -> str:
    return html_module.escape(str(text or ""))


def _workspace_status(workspace: Workspace) -> str:
    keywords = (workspace.keywords or "").strip()
    return (
        f"🗂 <b>{_esc(workspace.name)}</b>\n\n"
        f"Тема: {_esc(workspace.topic) or '—'}\n"
        f"Ключевые слова: {_esc(keywords) if keywords else 'встроенный AI-набор'}\n"
        f"OpenRouter-ключ: {'✅ подключен' if workspace.openrouter_api_key else '❌ нет'}\n\n"
        "Команды:\n"
        "/set_topic ТЕКСТ — тема воркспейса\n"
        "/set_keywords слова, через запятую — фильтр релевантности\n"
        "/add_source — добавить источник\n"
        "/connect_openrouter — подключить ключ (в личке с ботом)\n"
        "/queue, /approved, /stats — очередь и статистика"
    )


async def _require_group_admin(message: Message) -> bool:
    if not await is_chat_admin(message.bot, message.chat.id, message.from_user.id):
        await message.answer("⛔ Только для администраторов группы.")
        return False
    return True


@router.message(Command("setup"))
async def cmd_setup(message: Message, bot_user: BotUser) -> None:
    if not is_group_chat(getattr(message, "chat", None)):
        await message.answer(
            "Команда /setup работает в группе: добавьте бота в группу вашей "
            "команды и вызовите /setup там."
        )
        return

    if not await _require_group_admin(message):
        return

    async with async_session() as session:
        workspace = await resolve_workspace(session, message.chat)
        if workspace is not None:
            await message.answer(_workspace_status(workspace))
            return

        workspace = Workspace(
            chat_id=message.chat.id,
            name=(message.chat.title or str(message.chat.id))[:255],
            owner_user_id=message.from_user.id,
        )
        session.add(workspace)
        await session.commit()

    logger.info(
        f"Workspace created for chat {message.chat.id} by user {message.from_user.id}"
    )
    await message.answer(
        "✅ Воркспейс создан!\n\n"
        "Дальше три шага:\n"
        "1. Задайте тему: <code>/set_topic Новости финтеха</code>\n"
        "2. При желании — свои ключевые слова фильтра: "
        "<code>/set_keywords финтех, банки, платежи</code>\n"
        "3. Владелец пишет боту в личку /connect_openrouter и подключает "
        "OpenRouter-ключ — без него генерация не запустится.\n\n"
        "Потом добавляйте источники: /add_source"
    )


@router.message(Command("workspace"))
async def cmd_workspace(message: Message, bot_user: BotUser) -> None:
    if not is_group_chat(getattr(message, "chat", None)):
        await message.answer("Команда /workspace работает в группе воркспейса.")
        return

    async with async_session() as session:
        workspace = await resolve_workspace(session, message.chat)

    if workspace is None:
        await message.answer("Воркспейс не настроен. Запустите /setup")
        return

    await message.answer(_workspace_status(workspace))


async def _set_workspace_field(
    message: Message,
    field: str,
    value: str,
    confirmation: str,
) -> None:
    if not is_group_chat(getattr(message, "chat", None)):
        await message.answer("Эта команда работает в группе воркспейса.")
        return
    if not await _require_group_admin(message):
        return

    async with async_session() as session:
        workspace = await resolve_workspace(session, message.chat)
        if workspace is None:
            await message.answer("Сначала настройте воркспейс: /setup")
            return
        setattr(workspace, field, value)
        await session.commit()

    await message.answer(confirmation)


@router.message(Command("set_topic"))
async def cmd_set_topic(message: Message, bot_user: BotUser) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Использование: <code>/set_topic Новости финтеха и банков</code>"
        )
        return
    topic = parts[1].strip()
    await _set_workspace_field(
        message, "topic", topic, f"✅ Тема воркспейса: {_esc(topic)}"
    )


@router.message(Command("set_keywords"))
async def cmd_set_keywords(message: Message, bot_user: BotUser) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Использование: <code>/set_keywords финтех, банки, платежи</code>\n"
            "Слова через запятую; они заменят встроенный фильтр релевантности."
        )
        return
    keywords = parts[1].strip()
    await _set_workspace_field(
        message,
        "keywords",
        keywords,
        f"✅ Ключевые слова фильтра обновлены: {_esc(keywords)}",
    )


# ---------------------------------------------------------------------------
# /connect_openrouter — BYOK, только в личке
# ---------------------------------------------------------------------------

@router.message(Command("connect_openrouter"))
async def cmd_connect_openrouter(
    message: Message,
    bot_user: BotUser,
    state: FSMContext,
) -> None:
    if is_group_chat(getattr(message, "chat", None)):
        await message.answer(
            "Ключ передается только в личке: напишите мне в личные сообщения "
            "и повторите /connect_openrouter"
        )
        return

    async with async_session() as session:
        workspaces = (
            await session.scalars(
                select(Workspace).where(
                    Workspace.owner_user_id == message.from_user.id,
                    Workspace.chat_id.is_not(None),
                )
            )
        ).all()

    if not workspaces:
        await message.answer(
            "У вас нет воркспейсов. Добавьте бота в группу и запустите там /setup"
        )
        return

    rows = [
        [
            InlineKeyboardButton(
                text=workspace.name,
                callback_data=f"wkkey:{workspace.id}",
            )
        ]
        for workspace in workspaces
    ]
    await message.answer(
        "Выберите воркспейс для подключения OpenRouter-ключа:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("wkkey:"))
async def on_connect_key_workspace(
    callback: CallbackQuery,
    state: FSMContext,
    bot_user: BotUser,
) -> None:
    try:
        workspace_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Invalid data", show_alert=True)
        return

    async with async_session() as session:
        workspace = await session.get(Workspace, workspace_id)

    if workspace is None or workspace.owner_user_id != callback.from_user.id:
        await callback.answer("Воркспейс не найден", show_alert=True)
        return

    await state.set_state(ConnectKeyStates.waiting_for_key)
    await state.update_data(workspace_id=workspace_id)
    await callback.message.answer(
        f"Пришлите OpenRouter API-ключ для «{_esc(workspace.name)}» "
        "(начинается с <code>sk-or-</code>).\n"
        "Сообщение с ключом я удалю. Отправьте <b>отмена</b> для отмены."
    )
    await callback.answer()


@router.message(ConnectKeyStates.waiting_for_key)
async def on_openrouter_key(
    message: Message,
    state: FSMContext,
    bot_user: BotUser,
) -> None:
    text = (message.text or "").strip()

    if text.startswith("/") or text.lower() == "отмена":
        await state.clear()
        await message.answer("Подключение ключа отменено.")
        return

    if not text.startswith("sk-or-") or len(text) < 20:
        await message.answer(
            "Это не похоже на OpenRouter-ключ (ожидаю <code>sk-or-...</code>). "
            "Пришлите ключ или напишите «отмена»."
        )
        return

    data = await state.get_data()
    workspace_id = data.get("workspace_id")
    await state.clear()

    async with async_session() as session:
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_user_id != message.from_user.id:
            await message.answer("Воркспейс не найден.")
            return
        workspace.openrouter_api_key = text
        await session.commit()
        workspace_name = workspace.name

    # The key must not stay in the chat history
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Failed to delete OpenRouter key message: {e}")

    logger.info(
        f"OpenRouter key connected for workspace #{workspace_id} "
        f"by user {message.from_user.id}"
    )
    await message.answer(
        f"✅ Ключ подключен к «{_esc(workspace_name)}». "
        "Генерация будет идти через него."
    )
