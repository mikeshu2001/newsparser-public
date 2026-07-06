"""FSM states for article moderation flow."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ModerationStates(StatesGroup):
    waiting_for_edit_comment = State()
