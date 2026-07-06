import asyncio
import json
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.redis import RedisStorage
from loguru import logger

from app.config import settings
from app.database.database import close, ensure_schema, seed_data
from app.handlers import setup_routers
from app.middlewares import setup_middlewares
from app.services.notifier import set_bot
from app.services.scheduler import start_scheduler, stop_scheduler


def _json_sink(message: str) -> None:
    """Structured JSON log sink for production."""
    record = message.record
    entry = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": record["name"],
        "function": record["function"],
        "message": record["message"],
    }
    if record["exception"]:
        entry["exception"] = str(record["exception"])
    sys.stderr.write(json.dumps(entry, ensure_ascii=False) + "\n")


def setup_logging() -> None:
    logger.remove()
    if settings.log_format == "json":
        logger.add(
            _json_sink,
            level=settings.log_level,
        )
    else:
        logger.add(
            sys.stderr,
            level=settings.log_level,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> — <level>{message}</level>",
        )


async def on_startup(bot: Bot) -> None:
    await ensure_schema()
    await seed_data()
    set_bot(bot)
    if settings.scheduler_enabled:
        start_scheduler()
    else:
        logger.warning("Scheduler disabled by SCHEDULER_ENABLED=false")
    me = await bot.get_me()
    logger.info(f"Bot started: @{me.username}")


async def on_shutdown(bot: Bot) -> None:
    logger.info("Shutting down gracefully...")
    stop_scheduler()
    await close()
    logger.info("Bot stopped")


async def main() -> None:
    setup_logging()

    storage = RedisStorage.from_url(settings.redis_url)
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher(storage=storage)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    setup_middlewares(dp)
    setup_routers(dp)

    # SIGTERM/SIGINT are handled by aiogram's start_polling itself
    # (handle_signals=True): it stops polling and runs the shutdown hooks.
    # Registering our own handlers here would be dead code — start_polling
    # overwrites them.
    logger.info("Starting polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
