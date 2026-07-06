import html as html_module

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.database.models import BotUser
from app.services.health import collect_health_report, format_health_report
from app.services.workspaces import is_group_chat

router = Router()


@router.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery) -> None:
    await callback.answer()

_HELP_SECTIONS: dict[str, str] = {
    "all": (
        "<b>AI News Aggregator Bot</b>\n\n"
        "Автоматический сбор и обработка новостей про AI/нейросети.\n\n"
        "<b>Общие команды:</b>\n"
        "/start — Начало работы\n"
        "/help — Справка\n"
        "/approved — Одобренные статьи (за 7 дней)\n"
        "/queue — Кластеры в очереди на генерацию\n"
        "/stats — Статистика\n"
    ),
    "group": (
        "<b>Воркспейс группы</b>\n\n"
        "Бот собирает новости по вашей теме и приносит черновики на модерацию "
        "прямо в эту группу.\n\n"
        "/setup — создать воркспейс\n"
        "/workspace — статус и настройки\n"
        "/set_topic ТЕКСТ — тема\n"
        "/set_keywords слова, через запятую — фильтр\n"
        "/set_prompt — редакционный промпт (админы группы)\n"
        "/add_source — добавить источник (админы группы)\n"
        "/sources — список источников\n"
        "/queue — очередь кластеров\n"
        "/approved — одобренные статьи\n"
        "/stats — статистика\n"
        "/connect_openrouter — ключ OpenRouter (в личке с ботом)\n"
    ),
    "admin": (
        "\n<b>Администрирование:</b>\n"
        "/health — Статус системы\n"
        "/add_source — Добавить источник\n"
        "/sources — Список источников (вкл/выкл/удалить)\n"
        "/set_threshold N — Установить порог скоринга\n"
        "/set_prompt — Изменить промпт генерации\n"
        "/add_user ID РОЛЬ — Добавить пользователя\n"
    ),
}


@router.message(CommandStart())
async def cmd_start(message: Message, bot_user: BotUser) -> None:
    if is_group_chat(getattr(message, "chat", None)):
        await message.answer(_HELP_SECTIONS["group"], parse_mode="HTML")
        return

    role_label = {"admin": "Администратор", "moderator": "Модератор", "viewer": "Верстальщик"}
    first_name = html_module.escape(message.from_user.first_name or "")
    role = html_module.escape(role_label.get(bot_user.role, bot_user.role))
    await message.answer(
        f"Привет, {first_name}!\n\n"
        f"Ваша роль: <b>{role}</b>\n\n"
        "Используйте /help для списка команд.",
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message, bot_user: BotUser) -> None:
    if is_group_chat(getattr(message, "chat", None)):
        await message.answer(_HELP_SECTIONS["group"], parse_mode="HTML")
        return

    text = _HELP_SECTIONS["all"]
    if bot_user.role == "admin":
        text += _HELP_SECTIONS["admin"]
    await message.answer(text, parse_mode="HTML")


@router.message(Command("health"))
async def cmd_health(message: Message, bot_user: BotUser) -> None:
    if bot_user.role != "admin":
        await message.answer("⛔ Только для администраторов.")
        return

    from app.services import scheduler as scheduler_service

    report = await collect_health_report(
        last_cycle_at=scheduler_service.last_cycle_at,
        last_cycle_errors=scheduler_service.last_cycle_errors,
    )
    await message.answer(format_health_report(report), parse_mode="HTML")
