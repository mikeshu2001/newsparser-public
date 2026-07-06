from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.database.models import GeneratedArticle, NewsCluster, RawArticle, Setting, Source
from app.services import ai_providers, content_generator
from app.services.content_generator import _parse_response


class _ScalarResult:
    def __init__(self, rows: list[object]):
        self.rows = rows

    def all(self) -> list[object]:
        return self.rows

    def first(self) -> object | None:
        return self.rows[0] if self.rows else None


class _FakeSession:
    def __init__(self, scalar_results: list[list[object]], workspace: object = None):
        self.scalar_results = list(scalar_results)
        self.added: list[object] = []
        self.flushed = False
        self.workspace = workspace

    async def get(self, model: object, key: object) -> object:
        return self.workspace

    async def scalars(self, statement: object) -> _ScalarResult:
        return _ScalarResult(self.scalar_results.pop(0))

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed = True
        for obj in self.added:
            if isinstance(obj, GeneratedArticle) and obj.id is None:
                obj.id = 100


def _cluster() -> NewsCluster:
    return NewsCluster(
        id=5,
        category="llm",
        news_type="product_launch",
        status="generating",
    )


def _raw_article() -> RawArticle:
    return RawArticle(
        id=1,
        source_id=10,
        title="OpenAI launches tool",
        content="Details about the launch",
        url="https://openai.com/news/tool",
        cluster_id=5,
        fetched_at=datetime.now(timezone.utc),
    )


def _source() -> Source:
    return Source(id=10, name="OpenAI Blog", url="https://example.com", type="rss")


def test_parse_response_extracts_headline_and_body() -> None:
    parsed = _parse_response(
        "ЗАГОЛОВОК: OpenAI показала новый инструмент\n"
        "ТЕКСТ: Первый абзац.\n\nВторой абзац."
    )

    assert parsed == {
        "headline": "OpenAI показала новый инструмент",
        "body": "Первый абзац.\n\nВторой абзац.",
        "summary": "",
    }


def test_parse_response_extracts_social_description() -> None:
    parsed = _parse_response(
        "ЗАГОЛОВОК: OpenAI показала новый инструмент\n\n"
        "ТЕКСТ: Первый абзац.\n\nВторой абзац.\n\n"
        "ОПИСАНИЕ: Короткое описание для соцсетей."
    )

    assert parsed == {
        "headline": "OpenAI показала новый инструмент",
        "body": "Первый абзац.\n\nВторой абзац.",
        "summary": "Короткое описание для соцсетей.",
    }


def test_parse_response_truncates_social_description() -> None:
    summary = "А" * 320

    parsed = _parse_response(
        "ЗАГОЛОВОК: Заголовок\n"
        "ТЕКСТ: Текст\n"
        f"ОПИСАНИЕ: {summary}"
    )

    assert len(parsed["summary"]) == 280


def test_parse_response_falls_back_to_first_line() -> None:
    parsed = _parse_response("Неформатный заголовок\nТекст без маркеров")

    assert parsed["headline"] == "Неформатный заголовок"
    assert parsed["body"] == "Неформатный заголовок\nТекст без маркеров"
    assert parsed["summary"] == ""


async def test_generate_article_uses_default_prompt_and_increments_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = _cluster()
    session = _FakeSession([
        [_raw_article()],
        [_source()],
        [
            Setting(key="news_prompt", value="invalid prompt without sources"),
            Setting(key="tone_of_voice", value="Деловой стиль"),
        ],
        [GeneratedArticle(id=9, cluster_id=cluster.id, headline="Old", body="Old", version=2)],
    ])
    monkeypatch.setattr(
        content_generator,
        "_DEFAULT_PROMPT",
        "Tone: {tone_of_voice}\nCategory: {category}\nType: {news_type}\n{sources_block}",
    )
    prompts: list[str] = []

    async def fake_generate(prompt: str, workspace: object = None) -> tuple[str, str]:
        prompts.append(prompt)
        return "ЗАГОЛОВОК: Новый запуск\nТЕКСТ: Основной текст", "fake-provider"

    monkeypatch.setattr(ai_providers, "generate", fake_generate)

    article = await content_generator.generate_article(
        session,
        cluster,
        edit_comment="Добавь цифры",
    )

    assert article is not None
    assert article.id == 100
    assert article.version == 3
    assert article.ai_provider == "fake-provider"
    assert article.edit_comment == "Добавь цифры"
    assert article.headline == "Новый запуск"
    assert cluster.status == "pending_review"
    assert session.added == [article]
    assert session.flushed is True
    assert "Источник 1 (OpenAI Blog)" in prompts[0]
    assert "Ссылка: https://openai.com/news/tool" in prompts[0]
    assert "Дополнительные правки от редактора: Добавь цифры" in prompts[0]


async def test_generate_article_returns_none_without_usable_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = _cluster()
    session = _FakeSession([
        [_raw_article()],
        [_source()],
        [Setting(key="news_prompt", value="invalid prompt")],
    ])
    monkeypatch.setattr(content_generator, "_DEFAULT_PROMPT", "")

    article = await content_generator.generate_article(session, cluster)

    assert article is None
    assert session.added == []
    assert cluster.status == "generating"


async def test_generate_article_returns_none_when_ai_provider_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = _cluster()
    session = _FakeSession([
        [_raw_article()],
        [_source()],
        [Setting(key="news_prompt", value="{sources_block}")],
    ])

    async def fail_generate(prompt: str, workspace: object = None) -> tuple[str, str]:
        raise ai_providers.AllProvidersFailedError("down")

    monkeypatch.setattr(ai_providers, "generate", fail_generate)

    article = await content_generator.generate_article(session, cluster)

    assert article is None
    assert session.added == []
    assert cluster.status == "generating"
