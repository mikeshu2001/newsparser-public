"""Workspace resolution for the multi-tenant Telegram surface."""

from __future__ import annotations

from typing import Optional

from aiogram import Bot
from aiogram.types import Chat
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DEFAULT_WORKSPACE_ID, Workspace

GROUP_CHAT_TYPES = ("group", "supergroup")

# Telegram chat-member statuses that may manage a group workspace
_CHAT_ADMIN_STATUSES = ("administrator", "creator")


def is_group_chat(chat: Optional[Chat]) -> bool:
    return chat is not None and getattr(chat, "type", None) in GROUP_CHAT_TYPES


async def resolve_workspace(
    session: AsyncSession,
    chat: Chat,
) -> Optional[Workspace]:
    """Group chat -> its workspace (None until /setup); DM -> the default."""
    if is_group_chat(chat):
        return await session.scalar(
            select(Workspace).where(Workspace.chat_id == chat.id)
        )
    return await session.get(Workspace, DEFAULT_WORKSPACE_ID)


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Telegram group admins manage their workspace; failures deny access."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return getattr(member, "status", None) in _CHAT_ADMIN_STATUSES
