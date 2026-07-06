from __future__ import annotations

from app.parsers.base import ParsedArticle
from app.services.filter import is_relevant


def test_high_weight_source_bypasses_keyword_filter() -> None:
    article = ParsedArticle(title="Quarterly company update", content="No AI terms here")

    assert is_relevant(article, source_weight=9)


def test_broad_source_matches_english_keyword_in_title() -> None:
    article = ParsedArticle(title="OpenAI announces new model")

    assert is_relevant(article, source_weight=5)


def test_broad_source_matches_russian_keyword_in_content() -> None:
    article = ParsedArticle(
        title="Новый сервис для дизайнеров",
        content="Команда добавила генерация изображений для маркетинга.",
    )

    assert is_relevant(article, source_weight=5)


def test_keyword_after_first_500_content_chars_is_ignored() -> None:
    article = ParsedArticle(
        title="Regular market update",
        content=("x" * 501) + " OpenAI",
    )

    assert not is_relevant(article, source_weight=5)


def test_business_words_alone_do_not_pass_filter() -> None:
    """Regression: generic business words let any corporate story through."""
    article = ParsedArticle(
        title="Sony unveils new gaming console lineup",
        content="The acquisition raised the company's valuation after funding.",
    )

    assert not is_relevant(article, source_weight=5)


def test_keywords_match_whole_words_only() -> None:
    """Regression: substring hits ('raised'⊂'praised', 'udio'⊂'studio')."""
    for title in [
        "Critics praised the new exploration game",
        "Studio tour: tackling the backlog",
    ]:
        assert not is_relevant(ParsedArticle(title=title), source_weight=5)


def test_prefix_stem_matches_inflected_forms() -> None:
    article = ParsedArticle(title="Вышла мультимодальная версия сервиса")

    assert is_relevant(article, source_weight=5)


def test_workspace_keywords_replace_builtin_set() -> None:
    """Multi-tenant contract: a workspace's own keywords fully define its
    relevance filter; the built-in AI set no longer applies to it."""
    from types import SimpleNamespace

    workspace = SimpleNamespace(id=7, keywords="крипта, биткоин\nDeFi")
    crypto = ParsedArticle(title="Биткоин обновил исторический максимум")
    ai = ParsedArticle(title="OpenAI announces new model")

    assert is_relevant(crypto, 5, workspace)
    assert not is_relevant(ai, 5, workspace)
    assert is_relevant(ai, 5, None)


def test_broad_source_rejects_noise_without_ai_keywords() -> None:
    article = ParsedArticle(
        title="Local cafe opens second location",
        content="The team shared a menu update and opening hours.",
    )

    assert not is_relevant(article, source_weight=5)
