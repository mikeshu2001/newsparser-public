from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.database import database
from app.database.models import BotUser
from app.handlers import settings as settings_handler


class _DbAccessed(Exception):
    pass


class _FakeSeedSession:
    def __init__(self, users: list[BotUser] | None = None):
        self.users = {user.id: user for user in users or []}
        self.added: list[BotUser] = []

    async def get(self, model: object, user_id: int) -> BotUser | None:
        return self.users.get(user_id)

    def add(self, user: BotUser) -> None:
        self.added.append(user)
        self.users[user.id] = user


class _Message:
    def __init__(self, text: str):
        self.text = text
        self.from_user = SimpleNamespace(id=123)
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        self.answers.append(text)


def _admin_user() -> BotUser:
    return BotUser(id=1, role="admin", is_active=True)


def _session_factory_that_fails() -> object:
    raise _DbAccessed("DB session was opened")


async def test_seed_admin_users_restores_existing_bootstrap_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = BotUser(id=100, role="viewer", is_active=False)
    session = _FakeSeedSession([user])
    monkeypatch.setattr(database.settings, "admin_user_ids", [100])

    await database._seed_admin_users(session)

    assert user.role == "admin"
    assert user.is_active is True
    assert session.added == []


async def test_seed_admin_users_creates_missing_bootstrap_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSeedSession()
    monkeypatch.setattr(database.settings, "admin_user_ids", [200])

    await database._seed_admin_users(session)

    assert len(session.added) == 1
    assert session.added[0].id == 200
    assert session.added[0].role == "admin"
    assert session.added[0].is_active is True


async def test_add_user_rejects_bootstrap_admin_demotion_before_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings_handler.app_settings, "admin_user_ids", [300])
    monkeypatch.setattr(settings_handler, "async_session", _session_factory_that_fails)
    message = _Message("/add_user 300 viewer")

    await settings_handler.cmd_add_user(message, _admin_user())

    assert message.answers == [
        "Нельзя понизить bootstrap-администратора из ADMIN_USER_IDS."
    ]


async def test_add_user_allows_bootstrap_admin_admin_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings_handler.app_settings, "admin_user_ids", [300])
    monkeypatch.setattr(settings_handler, "async_session", _session_factory_that_fails)
    message = _Message("/add_user 300 admin")

    with pytest.raises(_DbAccessed):
        await settings_handler.cmd_add_user(message, _admin_user())
