from __future__ import annotations

from app.utils.text import extract_entities, has_trigger_phrases, normalize_title


def test_normalize_title_removes_stop_words_punctuation_and_accents() -> None:
    assert normalize_title("The New Café GPT-5: Launch for AI!") == "cafe gpt 5 launch ai"


def test_extract_entities_finds_known_and_versioned_tokens() -> None:
    entities = extract_entities("OpenAI launches GPT-5 with NovaMind and RTX 5090")

    assert "openai" in entities
    assert "gpt-5" in entities
    assert "novamind" in entities
    assert "rtx 5090" in entities


def test_extract_entities_skips_calendar_and_finance_tokens() -> None:
    """Regression: 'Q3'/'IPO' as entities merged unrelated earnings stories."""
    assert extract_entities("Nvidia Q3 earnings and IPO plans") == {"nvidia"}


def test_has_trigger_phrases_handles_english_and_russian() -> None:
    assert has_trigger_phrases("OpenAI is launching a new model today")
    assert has_trigger_phrases("Компания выпустили новую модель")
    assert not has_trigger_phrases("Quarterly research notes and market context")
