"""Inline keyboard for article moderation."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def moderation_keyboard(article_id: int) -> InlineKeyboardMarkup:
    """Build inline keyboard with 4 moderation actions."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Редактировать",
                    callback_data=f"mod:edit:{article_id}",
                ),
                InlineKeyboardButton(
                    text="🔄 Перегенерировать",
                    callback_data=f"mod:regen:{article_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✅ Готово",
                    callback_data=f"mod:approve:{article_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"mod:reject:{article_id}",
                ),
            ],
        ]
    )
