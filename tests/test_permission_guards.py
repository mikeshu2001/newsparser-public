from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable

import pytest

from app.database.models import BotUser
from app.handlers import moderation, settings as settings_handler, sources, start
from app.services.health import HealthReport, HealthStatus


class _DbAccessed(Exception):
    pass


class _Callback:
    def __init__(self, data: str):
        self.data = data
        self.from_user = SimpleNamespace(id=123)
        self.message = SimpleNamespace(
            html_text="original",
            edit_text=self._message_edit_text,
            answer=self._message_answer,
        )
        self.answers: list[tuple[str | None, bool]] = []
        self.message_answers: list[str] = []
        self.edits: list[str] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def _message_answer(self, text: str, **kwargs: object) -> None:
        self.message_answers.append(text)

    async def _message_edit_text(self, text: str, **kwargs: object) -> None:
        self.edits.append(text)


class _Message:
    def __init__(self, text: str = "text"):
        self.text = text
        self.from_user = SimpleNamespace(id=123, first_name="Test")
        self.answers: list[tuple[str, dict[str, object]]] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        self.answers.append((text, kwargs))


class _State:
    async def set_state(self, state: object) -> None:
        raise AssertionError("state should not be touched")

    async def update_data(self, **kwargs: object) -> None:
        raise AssertionError("state should not be touched")


class _TrackingState:
    def __init__(self):
        self.cleared = False

    async def clear(self) -> None:
        self.cleared = True

    async def get_data(self) -> dict[str, object]:
        raise AssertionError("state data should not be read")

    async def set_state(self, state: object) -> None:
        raise AssertionError("state should not advance")

    async def update_data(self, **kwargs: object) -> None:
        raise AssertionError("state should not update")


@dataclass
class _HandlerCase:
    name: str
    callback: Callable[..., object]
    data: str
    needs_state: bool = False


def _user(role: str) -> BotUser:
    return BotUser(id=1, role=role, is_active=True)


def _session_factory_that_fails() -> object:
    raise _DbAccessed("DB session was opened")


@pytest.mark.parametrize(
    "case",
    [
        _HandlerCase("approve", moderation.on_approve, "mod:approve:1"),
        _HandlerCase("reject", moderation.on_reject, "mod:reject:1"),
        _HandlerCase("regen", moderation.on_regen, "mod:regen:1"),
        _HandlerCase("edit", moderation.on_edit, "mod:edit:1", needs_state=True),
    ],
    ids=lambda case: case.name,
)
async def test_viewer_cannot_use_moderation_callbacks_before_db(
    monkeypatch: pytest.MonkeyPatch,
    case: _HandlerCase,
) -> None:
    monkeypatch.setattr(moderation, "async_session", _session_factory_that_fails)
    callback = _Callback(case.data)

    if case.needs_state:
        await case.callback(callback, _State(), _user("viewer"))
    else:
        await case.callback(callback, _user("viewer"))

    assert callback.answers == [("Недостаточно прав для модерации", True)]


async def test_moderator_moderation_callback_reaches_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(moderation, "async_session", _session_factory_that_fails)

    with pytest.raises(_DbAccessed):
        await moderation.on_approve(_Callback("mod:approve:1"), _user("moderator"))


@pytest.mark.parametrize(
    ("handler", "data"),
    [
        (sources.on_sources_page, "src:page:1"),
        (sources.on_source_toggle, "src:toggle:1"),
        (sources.on_source_delete, "src:delete:1"),
    ],
)
async def test_non_admin_cannot_use_source_callbacks_before_db(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[..., object],
    data: str,
) -> None:
    monkeypatch.setattr(sources, "async_session", _session_factory_that_fails)
    callback = _Callback(data)

    await handler(callback, _user("moderator"))

    assert callback.answers == [("⛔ Только для администраторов.", True)]


async def test_admin_source_toggle_reaches_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "async_session", _session_factory_that_fails)

    with pytest.raises(_DbAccessed):
        await sources.on_source_toggle(_Callback("src:toggle:1"), _user("admin"))


async def test_viewer_cannot_use_health_before_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_collect(*args: object, **kwargs: object) -> HealthReport:
        raise AssertionError("health collection should not run")

    monkeypatch.setattr(start, "collect_health_report", fail_collect)
    message = _Message()

    await start.cmd_health(message, _user("viewer"))

    assert message.answers == [("⛔ Только для администраторов.", {})]


async def test_admin_health_uses_shared_formatter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def collect(*args: object, **kwargs: object) -> HealthReport:
        return HealthReport(status=HealthStatus.OK, generated_at=None)  # type: ignore[arg-type]

    monkeypatch.setattr(start, "collect_health_report", collect)
    monkeypatch.setattr(start, "format_health_report", lambda report: "formatted")
    message = _Message()

    await start.cmd_health(message, _user("admin"))

    assert message.answers == [("formatted", {"parse_mode": "HTML"})]


async def test_non_admin_prompt_fsm_continuation_is_denied_before_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings_handler,
        "async_session",
        _session_factory_that_fails,
    )
    state = _TrackingState()
    message = _Message("new prompt")

    await settings_handler.on_prompt_text(message, state, _user("viewer"))

    assert state.cleared is True
    assert message.answers == [("⛔ Только для администраторов.", {})]


async def test_non_admin_add_source_callback_fsm_continuation_is_denied() -> None:
    state = _TrackingState()
    callback = _Callback("addsrc:type:rss")

    await sources.on_type_chosen(callback, state, _user("moderator"))

    assert state.cleared is True
    assert callback.answers == [("⛔ Только для администраторов.", True)]


async def test_non_admin_add_source_cancel_is_denied_without_editing() -> None:
    state = _TrackingState()
    callback = _Callback("addsrc:cancel")

    await sources.on_add_source_cancel(callback, state, _user("moderator"))

    assert state.cleared is True
    assert callback.answers == [("⛔ Только для администраторов.", True)]
    assert callback.edits == []


async def test_admin_add_source_cancel_keeps_existing_behavior() -> None:
    state = _TrackingState()
    callback = _Callback("addsrc:cancel")

    await sources.on_add_source_cancel(callback, state, _user("admin"))

    assert state.cleared is True
    assert callback.edits == ["Добавление источника отменено."]
    assert callback.answers == [(None, False)]


async def test_non_admin_add_source_message_fsm_continuation_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_validate(*args: object, **kwargs: object) -> tuple[bool, str]:
        raise AssertionError("source validation should not run")

    monkeypatch.setattr(sources, "_validate_source", fail_validate)
    state = _TrackingState()
    message = _Message("https://example.com/feed.xml")

    await sources.on_url_entered(message, state, _user("moderator"))

    assert state.cleared is True
    assert message.answers == [("⛔ Только для администраторов.", {})]


async def test_non_moderator_edit_comment_fsm_continuation_is_denied_before_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(moderation, "async_session", _session_factory_that_fails)
    state = _TrackingState()
    message = _Message("please edit")

    await moderation.on_edit_comment(message, state, _user("viewer"))

    assert state.cleared is True
    assert message.answers == [("Недостаточно прав для модерации", {})]


async def test_group_card_pressed_in_foreign_chat_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-tenant isolation: a group workspace's card only works inside
    its own group chat, regardless of the presser's bot role."""
    from app.database.models import GeneratedArticle, NewsCluster, Workspace

    article = GeneratedArticle(
        id=9, cluster_id=5, status="draft", version=1, headline="h", body="b"
    )
    cluster = NewsCluster(id=5, workspace_id=7, status="pending_review")
    foreign_workspace = SimpleNamespace(id=7, chat_id=-100777)

    class _WsSession:
        async def __aenter__(self) -> "_WsSession":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, model: object, key: object, **kwargs: object) -> object:
            if model is Workspace:
                return foreign_workspace
            return {GeneratedArticle: article, NewsCluster: cluster}.get(model)

        async def scalar(self, statement: object) -> int:
            return 1

        async def commit(self) -> None:
            raise AssertionError("no mutation may happen for a foreign chat")

    monkeypatch.setattr(moderation, "async_session", lambda: _WsSession())
    callback = _Callback("mod:approve:9")
    callback.message.chat = SimpleNamespace(type="supergroup", id=-100999)

    await moderation.on_approve(callback, _user("admin"))

    assert callback.answers == [
        ("Эта карточка принадлежит другому воркспейсу.", True)
    ]
