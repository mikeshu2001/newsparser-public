from aiogram import Dispatcher

from app.middlewares.auth import AuthMiddleware
from app.middlewares.logging import LoggingMiddleware
from app.middlewares.user import UserMiddleware


def setup_middlewares(dp: Dispatcher) -> None:
    dp.message.middleware(LoggingMiddleware())
    dp.callback_query.middleware(LoggingMiddleware())
    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())
    # UserMiddleware runs after Auth (bot_user is already in data)
    dp.message.middleware(UserMiddleware())
    dp.callback_query.middleware(UserMiddleware())
