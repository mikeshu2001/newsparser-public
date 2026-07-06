from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отмена")]],
        resize_keyboard=True,
    )


def confirm_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Подтвердить"), KeyboardButton(text="Отмена")]],
        resize_keyboard=True,
    )


def pagination_keyboard(
    prefix: str, current_page: int, total_pages: int
) -> list[InlineKeyboardButton]:
    buttons = []
    if current_page > 1:
        buttons.append(
            InlineKeyboardButton(text="◀️", callback_data=f"{prefix}:page:{current_page - 1}")
        )
    buttons.append(
        InlineKeyboardButton(
            text=f"{current_page}/{total_pages}", callback_data="noop"
        )
    )
    if current_page < total_pages:
        buttons.append(
            InlineKeyboardButton(text="▶️", callback_data=f"{prefix}:page:{current_page + 1}")
        )
    return buttons
