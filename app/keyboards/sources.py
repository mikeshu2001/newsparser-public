"""Keyboards for source management: type selection, categories, weight, list."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.keyboards.common import pagination_keyboard

# ---- Constants ----

SOURCE_TYPES: dict[str, str] = {
    "rss": "RSS",
    "telegram": "Telegram",
    "web": "Web-скрапинг",
    "sitemap": "Sitemap",
}

# Types offered in /add_source. 'web' and 'sitemap' require scraper_config,
# which the add dialogue does not collect — such sources could never parse.
ADDABLE_SOURCE_TYPES: tuple[str, ...] = ("rss", "telegram")

CATEGORIES: dict[str, str] = {
    "llm": "LLM",
    "image_gen": "Image Gen",
    "video_gen": "Video Gen",
    "audio_gen": "Audio Gen",
    "code": "Code AI",
    "robotics": "Robotics",
    "hardware": "Chips/Hardware",
    "research": "Research",
    "general": "General",
}

WEIGHTS: dict[int, str] = {
    3: "3 — низкий",
    5: "5 — средний",
    7: "7 — высокий",
    10: "10 — максимальный",
}

PAGE_SIZE = 5


# ---- Keyboards ----

def source_type_keyboard() -> InlineKeyboardMarkup:
    """Step 1: choose source type."""
    rows = []
    for key in ADDABLE_SOURCE_TYPES:
        rows.append([
            InlineKeyboardButton(
                text=SOURCE_TYPES[key], callback_data=f"addsrc:type:{key}"
            )
        ])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="addsrc:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def categories_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    """Step 4: multi-select categories.  Toggle marks with checkmark."""
    rows: list[list[InlineKeyboardButton]] = []
    items = list(CATEGORIES.items())
    # 3 per row
    for i in range(0, len(items), 3):
        row = []
        for key, label in items[i:i + 3]:
            mark = "✅ " if key in selected else ""
            row.append(
                InlineKeyboardButton(
                    text=f"{mark}{label}",
                    callback_data=f"addsrc:cat:{key}",
                )
            )
        rows.append(row)
    rows.append([InlineKeyboardButton(text="Готово ➡️", callback_data="addsrc:cat:done")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="addsrc:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def weight_keyboard() -> InlineKeyboardMarkup:
    """Step 5: choose source weight."""
    rows = []
    for value, label in WEIGHTS.items():
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"addsrc:weight:{value}")]
        )
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="addsrc:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sources_list_keyboard(
    sources: list,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    """Paginated source list with toggle / delete buttons per source."""
    rows: list[list[InlineKeyboardButton]] = []
    for src in sources:
        if src.active:
            toggle_text = f"⏸ {src.name}"
        else:
            toggle_text = f"▶️ {src.name}"
        rows.append([
            InlineKeyboardButton(
                text=toggle_text,
                callback_data=f"src:toggle:{src.id}",
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=f"src:delete:{src.id}",
            ),
        ])

    if total_pages > 1:
        rows.append(pagination_keyboard("src", page, total_pages))

    return InlineKeyboardMarkup(inline_keyboard=rows)
