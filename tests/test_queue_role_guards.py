from __future__ import annotations

from datetime import datetime, timezone

from app.database.models import BotUser, GeneratedArticle, NewsCluster
from app.handlers import viewer


class _Callback:
    def __init__(self, data: str):
        self.data = data
        self.message = _Message()
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class _Message:
    def __init__(self):
        self.answers: list[tuple[str, dict[str, object]]] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        self.answers.append((text, kwargs))


def _user(role: str) -> BotUser:
    return BotUser(id=1, role=role, is_active=True)


def _cluster() -> NewsCluster:
    return NewsCluster(
        id=42,
        topic="topic",
        topic_original="Topic",
        status="waiting",
        score=50,
        sources_count=1,
        first_seen_at=datetime.now(timezone.utc),
    )


class _FakeResult:
    def __init__(self, rows: list[object]):
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class _Session:
    def __init__(
        self,
        *,
        scalar_result: object = None,
        get_result: object = None,
        scalars_result: list[object] | None = None,
    ):
        self.scalar_result = scalar_result
        self.get_result = get_result
        self.scalars_result = scalars_result or []
        self.committed = False

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def scalar(self, statement: object) -> object:
        return self.scalar_result

    async def scalars(self, statement: object) -> _FakeResult:
        return _FakeResult(self.scalars_result)

    async def get(self, model: object, object_id: int) -> object:
        return self.get_result

    async def commit(self) -> None:
        self.committed = True


async def test_viewer_clear_callback_is_denied_before_db_access(
    monkeypatch,
) -> None:
    def fail_session() -> object:
        raise AssertionError("DB session should not be opened for unauthorized viewer")

    monkeypatch.setattr(viewer, "async_session", fail_session)
    callback = _Callback("queue:clear:42:1")

    await viewer.on_queue_clear(callback, _user("viewer"))

    assert callback.answers == [("Недостаточно прав для очистки очереди", True)]


async def test_moderator_clear_all_callback_is_denied_before_db_access(
    monkeypatch,
) -> None:
    def fail_session() -> object:
        raise AssertionError("DB session should not be opened for unauthorized moderator")

    monkeypatch.setattr(viewer, "async_session", fail_session)
    callback = _Callback("queue:clear_all:1")

    await viewer.on_queue_clear_all(callback, _user("moderator"))

    assert callback.answers == [("Недостаточно прав для очистки страницы", True)]


def test_queue_rows_hide_destructive_buttons_for_viewer() -> None:
    rows = viewer._build_queue_rows(
        [_cluster()], page=1, total_pages=1, can_clear_one=False, can_clear_page=False
    )

    assert rows == []


def test_queue_rows_allow_moderator_to_clear_one_only() -> None:
    rows = viewer._build_queue_rows(
        [_cluster()],
        page=1,
        total_pages=1,
        can_clear_one=True,
        can_clear_page=False,
    )
    callbacks = [button.callback_data for row in rows for button in row]

    assert callbacks == ["queue:clear:42:1"]


def test_queue_rows_allow_admin_to_clear_one_and_page() -> None:
    rows = viewer._build_queue_rows(
        [_cluster()], page=1, total_pages=1, can_clear_one=True, can_clear_page=True
    )
    callbacks = [button.callback_data for row in rows for button in row]

    assert callbacks == ["queue:clear:42:1", "queue:clear_all:1"]


async def test_approved_text_callback_rejects_non_approved_article(
    monkeypatch,
) -> None:
    pending_article = GeneratedArticle(
        id=7,
        cluster_id=1,
        headline="Draft",
        body="Secret draft body",
        status="draft",
    )
    session = _Session(scalar_result=None, get_result=pending_article)
    monkeypatch.setattr(viewer, "async_session", lambda: session)
    callback = _Callback("appr:text:7")

    await viewer.on_get_text(callback)

    assert callback.answers == [("Статья не найдена", True)]
    assert callback.message.answers == []


async def test_queue_clear_does_not_reject_pending_review_cluster(
    monkeypatch,
) -> None:
    cluster = _cluster()
    cluster.status = "pending_review"
    session = _Session(get_result=cluster)
    monkeypatch.setattr(viewer, "async_session", lambda: session)
    callback = _Callback("queue:clear:42:1")

    await viewer.on_queue_clear(callback, _user("moderator"))

    assert callback.answers == [("Кластер уже на модерации", True)]
    assert cluster.status == "pending_review"
    assert session.committed is False


def test_queue_rows_do_not_show_clear_actions_for_pending_review() -> None:
    cluster = _cluster()
    cluster.status = "pending_review"

    rows = viewer._build_queue_rows(
        [cluster],
        page=1,
        total_pages=1,
        can_clear_one=True,
        can_clear_page=True,
    )

    assert rows == []


def test_queue_rows_do_not_show_clear_actions_for_generating() -> None:
    cluster = _cluster()
    cluster.status = "generating"

    rows = viewer._build_queue_rows(
        [cluster],
        page=1,
        total_pages=1,
        can_clear_one=True,
        can_clear_page=True,
    )

    assert rows == []


async def test_queue_clear_does_not_reject_generating_cluster(
    monkeypatch,
) -> None:
    cluster = _cluster()
    cluster.status = "generating"
    session = _Session(get_result=cluster)
    monkeypatch.setattr(viewer, "async_session", lambda: session)
    callback = _Callback("queue:clear:42:1")

    await viewer.on_queue_clear(callback, _user("moderator"))

    assert callback.answers == [("Кластер уже на модерации", True)]
    assert cluster.status == "generating"
    assert session.committed is False


async def test_queue_clear_all_skips_pending_review_even_if_returned(
    monkeypatch,
) -> None:
    pending = _cluster()
    pending.id = 1
    pending.status = "pending_review"
    waiting = _cluster()
    waiting.id = 2
    waiting.status = "waiting"
    session = _Session(scalars_result=[pending, waiting])
    monkeypatch.setattr(viewer, "async_session", lambda: session)

    async def fake_send_queue_page(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(viewer, "_send_queue_page", fake_send_queue_page)
    callback = _Callback("queue:clear_all:1")

    await viewer.on_queue_clear_all(callback, _user("admin"))

    assert callback.answers == [("Очищено 1 кластеров", False)]
    assert pending.status == "pending_review"
    assert waiting.status == "rejected"
    assert session.committed is True
