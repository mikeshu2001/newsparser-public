"""Admin commands for bot settings: /set_threshold, /set_prompt, /add_user."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ForceReply, Message
from loguru import logger
from sqlalchemy import select

from app.config import settings as app_settings
from app.database.database import async_session
from app.database.models import BotUser, Setting, Workspace
from app.services.workspaces import is_chat_admin, is_group_chat, resolve_workspace

router = Router()


# ---------------------------------------------------------------------------
# FSM for /set_prompt (prompt can be multi-line)
# ---------------------------------------------------------------------------

class _PromptStates(StatesGroup):
    waiting_for_prompt = State()


# ---------------------------------------------------------------------------
# /set_threshold <value>
# ---------------------------------------------------------------------------

async def _group_settings_workspace(message: Message) -> Workspace | None:
    """Workspace when a group admin runs a settings command here."""
    async with async_session() as session:
        workspace = await resolve_workspace(session, message.chat)
    if workspace is None:
        await message.answer("Сначала настройте воркспейс: /setup")
        return None
    if not await is_chat_admin(message.bot, message.chat.id, message.from_user.id):
        await message.answer("⛔ Только для администраторов группы.")
        return None
    return workspace


@router.message(Command("set_threshold"))
async def cmd_set_threshold(message: Message, bot_user: BotUser) -> None:
    if is_group_chat(getattr(message, "chat", None)):
        workspace = await _group_settings_workspace(message)
        if workspace is None:
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            current = (
                str(workspace.score_threshold)
                if workspace.score_threshold is not None
                else "глобальный"
            )
            await message.answer(
                f"Текущий порог воркспейса: <b>{current}</b>\n\n"
                f"Использование: <code>/set_threshold 60</code>"
            )
            return
        try:
            value = int(parts[1].strip())
        except ValueError:
            await message.answer("Укажите целое число. Пример: <code>/set_threshold 60</code>")
            return
        if not 0 <= value <= 100:
            await message.answer("Порог должен быть от 0 до 100.")
            return
        async with async_session() as session:
            db_workspace = await session.get(Workspace, workspace.id)
            db_workspace.score_threshold = value
            await session.commit()
        await message.answer(f"✅ Порог воркспейса обновлён: <b>{value}</b>")
        return

    if bot_user.role != "admin":
        await message.answer("⛔ Только для администраторов.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        # Show current value
        async with async_session() as session:
            row = await session.get(Setting, "score_threshold")
            current = row.value if row else "50"
        await message.answer(
            f"Текущий порог: <b>{current}</b>\n\n"
            f"Использование: <code>/set_threshold 60</code>"
        )
        return

    try:
        value = int(parts[1].strip())
    except ValueError:
        await message.answer("Укажите целое число. Пример: <code>/set_threshold 60</code>")
        return

    if not 0 <= value <= 100:
        await message.answer("Порог должен быть от 0 до 100.")
        return

    async with async_session() as session:
        row = await session.get(Setting, "score_threshold")
        if row:
            row.value = str(value)
        else:
            session.add(Setting(key="score_threshold", value=str(value)))
        await session.commit()

    await message.answer(f"✅ Порог обновлён: <b>{value}</b>")
    logger.info(f"Score threshold set to {value} by user {message.from_user.id}")


# ---------------------------------------------------------------------------
# /set_prompt
# ---------------------------------------------------------------------------

@router.message(Command("set_prompt"))
async def cmd_set_prompt(message: Message, bot_user: BotUser, state: FSMContext) -> None:
    if is_group_chat(getattr(message, "chat", None)):
        workspace = await _group_settings_workspace(message)
        if workspace is None:
            return
        await state.set_state(_PromptStates.waiting_for_prompt)
        await state.update_data(workspace_id=workspace.id)
        await message.answer(
            "Отправьте новый промпт воркспейса ответом на это сообщение.\n\n"
            "Обязательная переменная: <code>{sources_block}</code>; "
            "опциональные: <code>{tone_of_voice}</code>, <code>{category}</code>, "
            "<code>{news_type}</code>\n\n"
            "Отправьте <b>отмена</b> для отмены.",
            reply_markup=ForceReply(selective=True),
        )
        return

    if bot_user.role != "admin":
        await message.answer("⛔ Только для администраторов.")
        return

    await state.set_state(_PromptStates.waiting_for_prompt)
    await message.answer(
        "Отправьте новый промпт для генерации статей.\n\n"
        "Доступные переменные:\n"
        "<code>{tone_of_voice}</code>, <code>{category}</code>, "
        "<code>{news_type}</code>, <code>{sources_block}</code>\n\n"
        "Отправьте <b>отмена</b> для отмены."
    )


@router.message(_PromptStates.waiting_for_prompt)
async def on_prompt_text(
    message: Message,
    state: FSMContext,
    bot_user: BotUser,
) -> None:
    if not is_group_chat(getattr(message, "chat", None)) and bot_user.role != "admin":
        await state.clear()
        await message.answer("⛔ Только для администраторов.")
        return

    text = message.text

    # Don't capture bot commands — cancel FSM and let the command be re-sent
    if text and text.startswith("/"):
        await state.clear()
        await message.answer("Установка промпта отменена. Повторите команду.")
        return

    await state.clear()

    if not text:
        await message.answer("Пустой текст. Промпт не изменён.")
        return

    if text.strip().lower() == "отмена":
        await message.answer("Отменено.")
        return

    data = await state.get_data()
    workspace_id = data.get("workspace_id")

    if workspace_id is not None:
        if "{sources_block}" not in text:
            await message.answer(
                "В промпте нет обязательной переменной <code>{sources_block}</code> — "
                "он не будет использоваться. Промпт не сохранён, повторите /set_prompt."
            )
            return
        async with async_session() as session:
            workspace = await session.get(Workspace, workspace_id)
            if workspace is None:
                await message.answer("Воркспейс не найден.")
                return
            workspace.news_prompt = text
            await session.commit()
        await message.answer(f"✅ Промпт воркспейса обновлён ({len(text)} символов).")
        return

    async with async_session() as session:
        row = await session.get(Setting, "news_prompt")
        if row:
            row.value = text
        else:
            session.add(Setting(key="news_prompt", value=text))
        await session.commit()

    await message.answer(f"✅ Промпт обновлён ({len(text)} символов).")
    logger.info(f"News prompt updated by user {message.from_user.id} ({len(text)} chars)")


# ---------------------------------------------------------------------------
# /add_user <telegram_id> <role>
# ---------------------------------------------------------------------------

_VALID_ROLES = {"admin", "moderator", "viewer"}


def _is_bootstrap_admin(user_id: int) -> bool:
    return user_id in set(app_settings.admin_user_ids)


@router.message(Command("add_user"))
async def cmd_add_user(message: Message, bot_user: BotUser) -> None:
    if is_group_chat(getattr(message, "chat", None)):
        await message.answer("Команда /add_user работает только в личке с ботом.")
        return
    if bot_user.role != "admin":
        await message.answer("⛔ Только для администраторов.")
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            "Использование: <code>/add_user TELEGRAM_ID РОЛЬ</code>\n\n"
            "Роли: admin, moderator, viewer\n"
            "Пример: <code>/add_user 123456789 moderator</code>"
        )
        return

    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("Telegram ID должен быть числом.")
        return

    role = parts[2].strip().lower()
    if role not in _VALID_ROLES:
        await message.answer(f"Неизвестная роль. Допустимые: {', '.join(sorted(_VALID_ROLES))}")
        return

    if _is_bootstrap_admin(user_id) and role != "admin":
        await message.answer("Нельзя понизить bootstrap-администратора из ADMIN_USER_IDS.")
        return

    async with async_session() as session:
        existing = await session.get(BotUser, user_id)
        if existing:
            old_role = existing.role
            existing.role = role
            existing.is_active = True
            await session.commit()
            await message.answer(
                f"✅ Пользователь {user_id} обновлён: {old_role} → <b>{role}</b>"
            )
        else:
            session.add(BotUser(id=user_id, role=role))
            await session.commit()
            await message.answer(f"✅ Пользователь {user_id} добавлен с ролью <b>{role}</b>")

    logger.info(f"User {user_id} set to role={role} by {message.from_user.id}")
