"""FSM states for the /add_source dialogue."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddSourceStates(StatesGroup):
    choosing_type = State()
    entering_url = State()
    entering_name = State()
    choosing_categories = State()
    choosing_weight = State()
