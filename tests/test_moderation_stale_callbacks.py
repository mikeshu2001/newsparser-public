from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.database.models import BotUser, GeneratedArticle, NewsCluster
from app.handlers import moderation
from app.services.generation_pipeline import is_manual_generation_claimed
from app.services.notifier import DeliveryResult


class _Callback:
    def __init__(self, data: str):
        self.data = data
        self.from_user = SimpleNamespace(id=123)
        self.message = _Message()
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class _Message:
    html_text = "<b>Draft card</b>"

    def __init__(self, text: str | None = None):
        self.text = text
        self.edits: list[tuple[str, dict[str, object]]] = []
        self.answers: list[str] = []

    async def edit_text(self, text: str, **kwargs: object) -> None:
        self.edits.append((text, kwargs))

    async def answer(self, text: str, **kwargs: object) -> None:
        self.answers.append(text)


class _FailingEditMessage(_Message):
    async def edit_text(self, text: str, **kwargs: object) -> None:
        self.edits.append((text, kwargs))
        raise RuntimeError("message is too long")


class _Session:
    def __init__(
        self,
        *,
        article: GeneratedArticle,
        cluster: NewsCluster,
        latest_version: int,
    ):
        self.article = article
        self.cluster = cluster
        self.latest_version = latest_version
        self.committed = False
        self.commit_count = 0

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def get(
        self,
        model: object,
        object_id: int,
        **kwargs: object,
    ) -> object | None:
        if model is GeneratedArticle:
            return self.article
        if model is NewsCluster:
            return self.cluster
        return None

    async def scalar(self, statement: object) -> int:
        return self.latest_version

    async def commit(self) -> None:
        self.committed = True
        self.commit_count += 1


class _State:
    def __init__(self, article_id: int):
        self.data = {"article_id": article_id}
        self.cleared = False

    async def get_data(self) -> dict[str, object]:
        return self.data

    async def clear(self) -> None:
        self.cleared = True


def _moderator() -> BotUser:
    return BotUser(id=1, role="moderator", is_active=True)


def _cluster() -> NewsCluster:
    return NewsCluster(id=10, status="pending_review")


def _article(*, version: int, status: str = "draft") -> GeneratedArticle:
    return GeneratedArticle(
        id=version,
        cluster_id=10,
        headline=f"Headline v{version}",
        body="Body",
        version=version,
        status=status,
    )


async def test_stale_article_version_cannot_be_approved(
    monkeypatch,
) -> None:
    article = _article(version=1)
    cluster = _cluster()
    session = _Session(article=article, cluster=cluster, latest_version=2)
    monkeypatch.setattr(moderation, "async_session", lambda: session)
    callback = _Callback("mod:approve:1")

    await moderation.on_approve(callback, _moderator())

    assert callback.answers == [
        ("Эта версия статьи устарела. Откройте последнюю карточку.", True)
    ]
    assert cluster.status == "pending_review"
    assert article.status == "draft"
    assert session.committed is False
    assert callback.message.edits == []


async def test_latest_draft_article_can_still_be_approved(
    monkeypatch,
) -> None:
    article = _article(version=2)
    cluster = _cluster()
    session = _Session(article=article, cluster=cluster, latest_version=2)
    monkeypatch.setattr(moderation, "async_session", lambda: session)
    callback = _Callback("mod:approve:2")

    await moderation.on_approve(callback, _moderator())

    assert callback.answers == [("Одобрено!", False)]
    assert cluster.status == "approved"
    assert article.status == "approved"
    assert article.moderated_by == 123
    assert session.committed is True
    assert callback.message.edits


async def test_regen_continues_when_status_message_edit_fails(
    monkeypatch,
) -> None:
    old_updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
    article = _article(version=2)
    cluster = _cluster()
    cluster.updated_at = old_updated_at
    session = _Session(article=article, cluster=cluster, latest_version=2)
    monkeypatch.setattr(moderation, "async_session", lambda: session)
    callback = _Callback("mod:regen:2")
    callback.message = _FailingEditMessage()
    generated: list[int] = []
    notified: list[int] = []

    async def generate(session_obj: object, cluster_obj: NewsCluster) -> GeneratedArticle:
        generated.append(cluster_obj.id)
        cluster_obj.status = "pending_review"
        return GeneratedArticle(
            id=3,
            cluster_id=cluster_obj.id,
            headline="New",
            body="Body",
            version=3,
        )

    async def notify(
        session_obj: object,
        new_article: GeneratedArticle,
        cluster_obj: NewsCluster,
        workspace: object = None,
    ) -> DeliveryResult:
        notified.append(new_article.id)
        return DeliveryResult(total_recipients=1, delivered_count=1, failed_count=0)

    monkeypatch.setattr(moderation, "generate_article", generate)
    monkeypatch.setattr(moderation, "send_to_moderators", notify)

    await moderation.on_regen(callback, _moderator())

    assert callback.message.edits == [("🔄 Перегенерация...", {"reply_markup": None})]
    assert callback.answers == [("Генерируем новую версию...", False)]
    assert generated == [cluster.id]
    assert notified == [3]
    assert cluster.status == "pending_review"
    assert cluster.updated_at > old_updated_at
    assert not is_manual_generation_claimed(cluster.id)
    assert session.commit_count == 3


async def test_edit_regen_refreshes_generating_timestamp(
    monkeypatch,
) -> None:
    old_updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
    article = _article(version=2)
    cluster = _cluster()
    cluster.updated_at = old_updated_at
    session = _Session(article=article, cluster=cluster, latest_version=2)
    monkeypatch.setattr(moderation, "async_session", lambda: session)
    message = _Message("Уточнить цифры")
    state = _State(article_id=article.id)
    generated: list[tuple[int, str | None]] = []
    notified: list[int] = []

    async def generate(
        session_obj: object,
        cluster_obj: NewsCluster,
        *,
        edit_comment: str | None = None,
    ) -> GeneratedArticle:
        generated.append((cluster_obj.id, edit_comment))
        cluster_obj.status = "pending_review"
        return GeneratedArticle(
            id=3,
            cluster_id=cluster_obj.id,
            headline="Edited",
            body="Body",
            version=3,
        )

    async def notify(
        session_obj: object,
        new_article: GeneratedArticle,
        cluster_obj: NewsCluster,
        workspace: object = None,
    ) -> DeliveryResult:
        notified.append(new_article.id)
        return DeliveryResult(total_recipients=1, delivered_count=1, failed_count=0)

    monkeypatch.setattr(moderation, "generate_article", generate)
    monkeypatch.setattr(moderation, "send_to_moderators", notify)

    await moderation.on_edit_comment(message, state, _moderator())

    assert state.cleared is True
    assert message.answers == ["🔄 Перегенерация с учётом правок..."]
    assert generated == [(cluster.id, "Уточнить цифры")]
    assert notified == [3]
    assert cluster.status == "pending_review"
    assert cluster.updated_at > old_updated_at
    assert not is_manual_generation_claimed(cluster.id)
    assert session.commit_count == 3


async def test_regen_failure_restores_keyboard_and_releases_manual_claim(
    monkeypatch,
) -> None:
    article = _article(version=2)
    cluster = _cluster()
    session = _Session(article=article, cluster=cluster, latest_version=2)
    monkeypatch.setattr(moderation, "async_session", lambda: session)
    callback = _Callback("mod:regen:2")

    async def generate(session_obj: object, cluster_obj: NewsCluster) -> None:
        return None

    monkeypatch.setattr(moderation, "generate_article", generate)

    await moderation.on_regen(callback, _moderator())

    assert callback.message.edits[0] == ("🔄 Перегенерация...", {"reply_markup": None})
    restored_text, restored_kwargs = callback.message.edits[1]
    assert restored_text == "<b>Draft card</b>"
    assert restored_kwargs["reply_markup"] is not None
    assert callback.message.answers == ["Не удалось перегенерировать. Попробуйте позже."]
    assert cluster.status == "pending_review"
    assert not is_manual_generation_claimed(cluster.id)
    assert session.commit_count == 2
