from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy import select

from app.database.database import async_session
from app.database.models import BotUser


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return

        async with async_session() as session:
            bot_user = await session.scalar(
                select(BotUser).where(BotUser.id == user.id)
            )

        chat = data.get("event_chat")
        if chat is not None and chat.type in ("group", "supergroup"):
            # Group chats are multi-tenant workspaces: Telegram membership is
            # the access boundary there, the DM allowlist does not apply.
            # Unknown members act as viewers (transient, not persisted).
            if bot_user is not None and not bot_user.is_active:
                return
            if bot_user is None:
                bot_user = BotUser(id=user.id, role="viewer", is_active=True)
            data["bot_user"] = bot_user
            data["user_role"] = bot_user.role
            return await handler(event, data)

        if bot_user is None or not bot_user.is_active:
            if isinstance(event, Message):
                await event.answer("У вас нет доступа к этому боту.")
            elif isinstance(event, CallbackQuery):
                await event.answer("У вас нет доступа к этому боту.", show_alert=True)
            return

        data["bot_user"] = bot_user
        data["user_role"] = bot_user.role
        return await handler(event, data)
