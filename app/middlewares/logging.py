from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from loguru import logger


class LoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        user_info = f"user={user.id}" if user else "user=unknown"
        event_type = type(event).__name__

        logger.debug(f"[{event_type}] {user_info}")

        try:
            result = await handler(event, data)
            return result
        except Exception as e:
            logger.exception(f"[{event_type}] {user_info} error: {e}")
            raise
