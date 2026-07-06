from typing import Optional
from urllib.parse import quote

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Bot
    bot_token: str
    admin_user_ids: list[int]

    # Database
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str
    postgres_password: str

    # Redis
    redis_host: str
    redis_port: int

    # AI Provider (OpenRouter)
    openrouter_api_key: Optional[str] = None
    openrouter_scoring_model: str = "anthropic/claude-haiku-4.5"
    openrouter_generation_model: str = "anthropic/claude-sonnet-4.6"

    # Dev-only AI Provider (local smoke-test stub)
    local_ai_provider_enabled: bool = False

    # Optional AI Provider (Codex CLI, uses official local Codex login/cache)
    codex_provider_enabled: bool = False
    codex_bin: str = "codex"
    codex_model: Optional[str] = None
    codex_timeout_seconds: int = 300
    codex_sandbox: str = "read-only"

    # Optional AI Provider (OAuth Bearer-token gateway, tried before OpenRouter)
    oauth_ai_base_url: Optional[str] = None
    oauth_ai_chat_completions_path: str = "/chat/completions"
    oauth_ai_access_token: Optional[str] = None
    oauth_ai_token_file: Optional[str] = None
    oauth_ai_scoring_model: Optional[str] = None
    oauth_ai_generation_model: Optional[str] = None
    oauth_ai_refresh_margin_seconds: int = 60

    # Optional OAuth login helper settings
    oauth_ai_authorization_url: Optional[str] = None
    oauth_ai_token_url: Optional[str] = None
    oauth_ai_client_id: Optional[str] = None
    oauth_ai_client_secret: Optional[str] = None
    oauth_ai_redirect_uri: str = "http://127.0.0.1:8765/callback"
    oauth_ai_scopes: Optional[str] = None

    # Parsing
    scheduler_enabled: bool = True
    parsing_interval_minutes: int = 15
    request_timeout_seconds: int = 30

    # Telegram RSS
    telegram_rss_service: str = "https://tg.i-c-a.su"

    # Logging
    log_level: str = "INFO"
    log_format: str = "pretty"  # "pretty" for dev, "json" for production

    @property
    def database_url(self) -> str:
        user = quote(self.postgres_user, safe="")
        password = quote(self.postgres_password, safe="")
        database = quote(self.postgres_db, safe="")
        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{database}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"

    @field_validator(
        "bot_token",
        "postgres_host",
        "postgres_db",
        "postgres_user",
        "postgres_password",
        "redis_host",
        "codex_bin",
        "codex_sandbox",
    )
    @classmethod
    def _required_string_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator(
        "openrouter_api_key",
        "codex_model",
        "oauth_ai_base_url",
        "oauth_ai_access_token",
        "oauth_ai_token_file",
        "oauth_ai_scoring_model",
        "oauth_ai_generation_model",
        "oauth_ai_authorization_url",
        "oauth_ai_token_url",
        "oauth_ai_client_id",
        "oauth_ai_client_secret",
        "oauth_ai_scopes",
        mode="before",
    )
    @classmethod
    def _blank_optional_string_as_none(cls, value: Optional[str]) -> Optional[str]:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("admin_user_ids")
    @classmethod
    def _admin_user_ids_not_empty(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("must contain at least one Telegram user ID")
        return value

    @model_validator(mode="after")
    def _at_least_one_ai_provider_configured(self) -> "Settings":
        oauth_configured = bool(
            self.oauth_ai_base_url
            and self.oauth_ai_generation_model
            and (self.oauth_ai_access_token or self.oauth_ai_token_file)
        )
        if not (
            self.local_ai_provider_enabled
            or self.codex_provider_enabled
            or oauth_configured
            or self.openrouter_api_key
        ):
            raise ValueError(
                "at least one AI provider must be configured: "
                "enable LOCAL_AI_PROVIDER_ENABLED for local smoke tests, "
                "enable CODEX_PROVIDER_ENABLED for Codex CLI, "
                "configure OAuth AI Gateway, or set OPENROUTER_API_KEY"
            )
        return self


settings = Settings()
