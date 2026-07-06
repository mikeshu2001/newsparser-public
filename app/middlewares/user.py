"""UserMiddleware — auto-save/update user info on every interaction."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.database.database import async_session
from app.database.models import BotUser


class UserMiddleware(BaseMiddleware):
    """Update username and first_name for known users on every request."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        bot_user: BotUser | None = data.get("bot_user")
        if bot_user is None:
            return await handler(event, data)

        # Update stored info if changed
        changed = False
        if bot_user.username != user.username:
            bot_user.username = user.username
            changed = True
        if bot_user.first_name != user.first_name:
            bot_user.first_name = user.first_name
            changed = True

        if changed:
            async with async_session() as session:
                db_user = await session.get(BotUser, bot_user.id)
                if db_user:
                    db_user.username = user.username
                    db_user.first_name = user.first_name
                    await session.commit()

        return await handler(event, data)
