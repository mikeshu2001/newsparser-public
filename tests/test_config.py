from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def _valid_settings_kwargs() -> dict[str, object]:
    return {
        "bot_token": "123456:test-token",
        "admin_user_ids": [123456789],
        "openrouter_api_key": "test-openrouter-key",
        "postgres_host": "localhost",
        "postgres_port": 5432,
        "postgres_db": "ainews_test",
        "postgres_user": "ainews_test",
        "postgres_password": "ainews_test_password",
        "redis_host": "localhost",
        "redis_port": 6379,
    }


def test_settings_accept_valid_required_runtime_values() -> None:
    settings = Settings(
        _env_file=None,
        **_valid_settings_kwargs(),
    )

    assert settings.bot_token == "123456:test-token"
    assert settings.admin_user_ids == [123456789]
    assert settings.openrouter_api_key == "test-openrouter-key"
    assert settings.oauth_ai_base_url is None
    assert settings.local_ai_provider_enabled is False


def test_settings_require_admin_user_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_USER_IDS", raising=False)

    with pytest.raises(ValidationError, match="Field required"):
        Settings(
            _env_file=None,
            **{
                key: value
                for key, value in _valid_settings_kwargs().items()
                if key != "admin_user_ids"
            },
        )


def test_settings_reject_empty_admin_user_ids() -> None:
    with pytest.raises(ValidationError, match="at least one Telegram user ID"):
        Settings(
            _env_file=None,
            **{
                **_valid_settings_kwargs(),
                "admin_user_ids": [],
            },
        )


def test_settings_require_database_and_redis_runtime_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in [
        "postgres_host",
        "postgres_port",
        "postgres_db",
        "postgres_user",
        "postgres_password",
        "redis_host",
        "redis_port",
    ]:
        monkeypatch.delenv(key.upper(), raising=False)
        values = _valid_settings_kwargs()
        values.pop(key)

        with pytest.raises(ValidationError, match="Field required"):
            Settings(_env_file=None, **values)


def test_database_url_escapes_postgres_credentials() -> None:
    settings = Settings(
        _env_file=None,
        **{
            **_valid_settings_kwargs(),
            "postgres_user": "user@name",
            "postgres_password": "p@ss:word/with#chars",
            "postgres_db": "ai/news",
        },
    )

    assert settings.database_url == (
        "postgresql+asyncpg://user%40name:p%40ss%3Aword%2Fwith%23chars"
        "@localhost:5432/ai%2Fnews"
    )


def test_settings_reject_blank_database_and_redis_values() -> None:
    for key in ["postgres_host", "postgres_db", "postgres_user", "postgres_password", "redis_host"]:
        with pytest.raises(ValidationError, match="must not be blank"):
            Settings(
                _env_file=None,
                **{
                    **_valid_settings_kwargs(),
                    key: " ",
                },
            )


def test_settings_require_at_least_one_ai_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(ValidationError, match="at least one AI provider"):
        Settings(
            _env_file=None,
            **{
                key: value
                for key, value in _valid_settings_kwargs().items()
                if key != "openrouter_api_key"
            },
        )


def test_settings_accept_local_ai_provider_without_openrouter() -> None:
    settings = Settings(
        _env_file=None,
        **{
            key: value
            for key, value in {
                **_valid_settings_kwargs(),
                "openrouter_api_key": " ",
                "local_ai_provider_enabled": True,
            }.items()
        },
    )

    assert settings.openrouter_api_key is None
    assert settings.local_ai_provider_enabled is True


def test_settings_accept_oauth_ai_provider_without_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    settings = Settings(
        _env_file=None,
        **{
            **{
                key: value
                for key, value in _valid_settings_kwargs().items()
                if key != "openrouter_api_key"
            },
            "oauth_ai_base_url": "https://gateway.example/v1",
            "oauth_ai_access_token": "oauth-token",
            "oauth_ai_generation_model": "gateway-model",
        },
    )

    assert settings.openrouter_api_key is None
    assert settings.oauth_ai_base_url == "https://gateway.example/v1"


def test_settings_accept_codex_provider_without_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    settings = Settings(
        _env_file=None,
        **{
            **{
                key: value
                for key, value in _valid_settings_kwargs().items()
                if key != "openrouter_api_key"
            },
            "codex_provider_enabled": True,
        },
    )

    assert settings.openrouter_api_key is None
    assert settings.codex_provider_enabled is True
    assert settings.codex_bin == "codex"


def test_settings_treat_blank_optional_oauth_values_as_unset() -> None:
    settings = Settings(
        _env_file=None,
        **{
            **_valid_settings_kwargs(),
            "oauth_ai_base_url": " ",
            "oauth_ai_access_token": "",
            "oauth_ai_token_file": " ",
        },
    )

    assert settings.oauth_ai_base_url is None
    assert settings.oauth_ai_access_token is None
    assert settings.oauth_ai_token_file is None
