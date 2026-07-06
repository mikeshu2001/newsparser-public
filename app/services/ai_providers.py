"""AI providers.

Optional OAuthAIProvider can be tried before OpenRouter when configured.
OpenRouter fallback uses two models:
- scoring model (cheap/fast): Claude Haiku 4.5
- generation model (quality): Claude Sonnet 4.6
"""

from __future__ import annotations

import abc
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import httpx
from loguru import logger

from app.config import settings
from app.database.models import DEFAULT_WORKSPACE_ID, Workspace


class AIProviderError(Exception):
    """Raised when an AI provider fails."""


class AllProvidersFailedError(AIProviderError):
    """Raised when every provider in the chain has failed."""


class BaseAIProvider(abc.ABC):
    """Abstract base for AI providers."""

    name: str = "base"

    @abc.abstractmethod
    def is_configured(self) -> bool:
        ...

    @abc.abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> str:
        ...


class OpenRouterProvider(BaseAIProvider):
    name = "openrouter"

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                api_key=settings.openrouter_api_key,
                base_url="https://openrouter.ai/api/v1",
            )
        return self._client

    def is_configured(self) -> bool:
        return bool(settings.openrouter_api_key)

    async def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> str:
        model = model or settings.openrouter_generation_model
        client = self._get_client()
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as e:
            logger.error(f"OpenRouter API error (model={model}): {e}")
            raise AIProviderError(f"OpenRouter: {e}") from e

        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise AIProviderError(
                f"OpenRouter returned empty content (model={model})"
            )
        return content


class LocalTestAIProvider(BaseAIProvider):
    """Dev-only deterministic provider for local Telegram smoke tests."""

    name = "local_test"

    def is_configured(self) -> bool:
        return settings.local_ai_provider_enabled

    async def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> str:
        if "Ответь строго в формате JSON" in prompt:
            return json.dumps({
                "type": "other",
                "importance": 5,
                "category": "general",
            })

        return (
            "ЗАГОЛОВОК: Тестовый черновик AI News\n\n"
            "ТЕКСТ:\n"
            "Это локальный тестовый черновик для проверки Telegram-бота. "
            "Он создан без обращения к внешнему AI API, чтобы можно было "
            "проверить очередь, модерацию, approve/reject и выдачу approved "
            "текста перед подключением реального OAuth AI gateway."
        )


class CodexCLIProvider(BaseAIProvider):
    """Optional provider that uses official local Codex CLI authentication."""

    name = "codex_cli"

    def is_configured(self) -> bool:
        return settings.codex_provider_enabled

    def _build_backend_prompt(self, prompt: str) -> str:
        return (
            "You are being used as a text-generation backend for a Telegram "
            "news moderation bot. Return only the requested final answer. "
            "Do not explain your reasoning, do not mention Codex, and do not "
            "include operational commentary. Preserve any exact output format "
            "requested by the prompt.\n\n"
            f"{prompt}"
        )

    def _build_command(self, output_path: str) -> list[str]:
        command = [
            settings.codex_bin,
            "exec",
            "--sandbox",
            settings.codex_sandbox,
            "--ephemeral",
            # The bot's workdir (e.g. /app in Docker) is not a git repo, and
            # codex exec refuses to run outside one without this flag.
            "--skip-git-repo-check",
            "--cd",
            os.getcwd(),
            "--output-last-message",
            output_path,
        ]
        if settings.codex_model:
            command.extend(["--model", settings.codex_model])
        command.append("-")
        return command

    async def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> str:
        with tempfile.NamedTemporaryFile(
            prefix="codex-ai-provider-",
            suffix=".txt",
            delete=False,
        ) as output_file:
            output_path = output_file.name

        command = self._build_command(output_path)
        backend_prompt = self._build_backend_prompt(prompt)

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(backend_prompt.encode("utf-8")),
                timeout=settings.codex_timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            if "process" in locals():
                process.kill()
                await process.wait()
            try:
                os.unlink(output_path)
            except OSError:
                pass
            raise AIProviderError(
                f"Codex CLI timed out after {settings.codex_timeout_seconds}s"
            ) from e
        except Exception as e:
            try:
                os.unlink(output_path)
            except OSError:
                pass
            raise AIProviderError(f"Codex CLI failed to start: {e}") from e

        if process.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            detail = stderr_text or stdout_text or f"exit code {process.returncode}"
            try:
                os.unlink(output_path)
            except OSError:
                pass
            raise AIProviderError(f"Codex CLI failed: {detail}")

        try:
            text = Path(output_path).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise AIProviderError(f"Codex CLI output was not readable: {e}") from e
        finally:
            try:
                os.unlink(output_path)
            except OSError:
                pass

        if not text:
            raise AIProviderError("Codex CLI returned an empty response")
        return text


class OAuthAIProvider(BaseAIProvider):
    """Generic OAuth Bearer-token provider for chat-completions gateways."""

    name = "oauth_ai"

    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self._transport = transport

    def is_configured(self) -> bool:
        return bool(
            settings.oauth_ai_base_url
            and settings.oauth_ai_generation_model
            and self._load_access_token()
        )

    def _load_access_token(self) -> Optional[str]:
        if settings.oauth_ai_access_token:
            return settings.oauth_ai_access_token.strip()

        token_payload = self._load_token_file_payload()
        if isinstance(token_payload, str):
            return token_payload.strip() or None
        if isinstance(token_payload, dict):
            return self._extract_access_token(token_payload)
        return None

    def _load_token_file_payload(self) -> Optional[object]:
        if not settings.oauth_ai_token_file:
            return None

        try:
            with open(settings.oauth_ai_token_file, encoding="utf-8") as token_file:
                token_data = token_file.read().strip()
        except OSError as e:
            logger.warning(f"OAuth AI token file is not readable: {e}")
            return None

        if not token_data:
            return None

        try:
            return json.loads(token_data)
        except json.JSONDecodeError:
            return token_data

    def _extract_access_token(self, payload: dict[str, object]) -> Optional[str]:
        token = payload.get("access_token")
        if isinstance(token, str) and token.strip():
            return token.strip()
        return None

    def _token_needs_refresh(self, payload: dict[str, object]) -> bool:
        expires_at = payload.get("expires_at")
        if expires_at is None:
            return False
        try:
            expires_at_timestamp = float(expires_at)
        except (TypeError, ValueError):
            return False
        refresh_at = expires_at_timestamp - settings.oauth_ai_refresh_margin_seconds
        return time.time() >= refresh_at

    def _can_refresh_token(self, payload: dict[str, object]) -> bool:
        return bool(
            payload.get("refresh_token")
            and settings.oauth_ai_token_url
            and settings.oauth_ai_client_id
            and settings.oauth_ai_token_file
        )

    def _write_token_file(self, payload: dict[str, object]) -> None:
        if not settings.oauth_ai_token_file:
            raise AIProviderError("OAuth AI: token file path is not configured")

        token_dir = os.path.dirname(settings.oauth_ai_token_file)
        if token_dir:
            os.makedirs(token_dir, exist_ok=True)

        serialized = json.dumps(payload, indent=2, sort_keys=True)
        fd = os.open(settings.oauth_ai_token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as token_file:
            token_file.write(serialized)
            token_file.write("\n")

    async def _refresh_access_token(self, payload: dict[str, object]) -> str:
        if not self._can_refresh_token(payload):
            raise AIProviderError("OAuth AI: access token is expired and cannot be refreshed")

        data = {
            "grant_type": "refresh_token",
            "refresh_token": str(payload["refresh_token"]),
            "client_id": settings.oauth_ai_client_id,
        }
        if settings.oauth_ai_client_secret:
            data["client_secret"] = settings.oauth_ai_client_secret

        try:
            async with httpx.AsyncClient(
                timeout=settings.request_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(settings.oauth_ai_token_url, data=data)
                response.raise_for_status()
                refreshed = response.json()
        except Exception as e:
            logger.error(f"OAuth AI token refresh failed: {e}")
            raise AIProviderError(f"OAuth AI token refresh: {e}") from e

        if not isinstance(refreshed, dict):
            raise AIProviderError("OAuth AI token refresh: response JSON must be an object")

        merged: dict[str, object] = {**payload, **refreshed}
        if "expires_in" in refreshed and "expires_at" not in refreshed:
            try:
                merged["expires_at"] = int(time.time()) + int(refreshed["expires_in"])
            except (TypeError, ValueError):
                pass

        access_token = self._extract_access_token(merged)
        if not access_token:
            raise AIProviderError("OAuth AI token refresh: response did not include access_token")

        try:
            self._write_token_file(merged)
        except OSError as e:
            logger.warning(
                "OAuth AI: refreshed token could not be persisted to "
                f"{settings.oauth_ai_token_file}: {e}"
            )
        return access_token

    async def _get_access_token(self) -> Optional[str]:
        if settings.oauth_ai_access_token:
            return settings.oauth_ai_access_token.strip()

        token_payload = self._load_token_file_payload()
        if isinstance(token_payload, str):
            return token_payload.strip() or None
        if not isinstance(token_payload, dict):
            return None

        access_token = self._extract_access_token(token_payload)
        if not access_token:
            return None
        if self._token_needs_refresh(token_payload):
            return await self._refresh_access_token(token_payload)
        return access_token

    def _chat_url(self) -> str:
        base_url = (settings.oauth_ai_base_url or "").rstrip("/") + "/"
        path = settings.oauth_ai_chat_completions_path.lstrip("/")
        return urljoin(base_url, path)

    def _default_model(self, use_scoring_model: bool) -> Optional[str]:
        if use_scoring_model:
            return settings.oauth_ai_scoring_model or settings.oauth_ai_generation_model
        return settings.oauth_ai_generation_model

    def _extract_text(self, payload: object) -> str:
        if not isinstance(payload, dict):
            raise AIProviderError("OAuth AI: response JSON must be an object")

        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first_choice = choices[0]
            if isinstance(first_choice, dict):
                message = first_choice.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content:
                        return content
                text = first_choice.get("text")
                if isinstance(text, str) and text:
                    return text

        for key in ("text", "content", "output_text"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value

        raise AIProviderError("OAuth AI: response did not contain generated text")

    async def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> str:
        access_token = await self._get_access_token()
        if not access_token:
            raise AIProviderError("OAuth AI: access token is not configured")

        actual_model = model or settings.oauth_ai_generation_model
        if not actual_model:
            raise AIProviderError("OAuth AI: generation model is not configured")

        payload = {
            "model": actual_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                timeout=settings.request_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._chat_url(),
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                return self._extract_text(response.json())
        except AIProviderError:
            raise
        except Exception as e:
            logger.error(f"OAuth AI API error (model={actual_model}): {e}")
            raise AIProviderError(f"OAuth AI: {e}") from e


# Singleton chain -------------------------------------------------------

_providers: list[BaseAIProvider] = [
    LocalTestAIProvider(),
    CodexCLIProvider(),
    OAuthAIProvider(),
    OpenRouterProvider(),
]

_MAX_RETRIES = 2
_RETRY_BASE_DELAY = 1.0  # seconds


def _is_transient(error: Exception) -> bool:
    """Check if the error is transient and worth retrying (429, 5xx, overloaded)."""
    err_str = str(error).lower()
    return any(code in err_str for code in ("429", "500", "502", "503", "529", "overloaded", "rate_limit"))


def _provider_default_model(
    provider: BaseAIProvider,
    *,
    use_scoring_model: bool,
) -> Optional[str]:
    if isinstance(provider, OAuthAIProvider):
        return provider._default_model(use_scoring_model)
    if use_scoring_model:
        return settings.openrouter_scoring_model
    return None


# BYOK: one cached OpenRouter client per workspace API key.
_workspace_clients: dict[str, object] = {}


def _openrouter_client(api_key: str):
    client = _workspace_clients.get(api_key)
    if client is None:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
        _workspace_clients[api_key] = client
    return client


async def _generate_with_workspace_key(
    prompt: str,
    *,
    api_key: str,
    model: str,
    max_tokens: int,
    temperature: float,
) -> tuple[str, str]:
    """Direct OpenRouter call with a workspace's own key (BYOK)."""
    client = _openrouter_client(api_key)
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as e:
            if attempt < _MAX_RETRIES and _is_transient(e):
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"workspace OpenRouter transient error "
                    f"(attempt {attempt + 1}/{_MAX_RETRIES + 1}), retry in {delay:.0f}s: {e}"
                )
                await asyncio.sleep(delay)
                continue
            logger.error(f"workspace OpenRouter error (model={model}): {e}")
            raise AllProvidersFailedError(f"OpenRouter (workspace key): {e}") from e

        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise AllProvidersFailedError(
                f"OpenRouter returned empty content (model={model})"
            )
        return content, "openrouter"

    raise AllProvidersFailedError("OpenRouter (workspace key): retries exhausted")


def _workspace_model(
    workspace: Workspace,
    *,
    use_scoring_model: bool,
) -> str:
    if use_scoring_model:
        return workspace.scoring_model or settings.openrouter_scoring_model
    return workspace.generation_model or settings.openrouter_generation_model


async def generate(
    prompt: str,
    *,
    model: Optional[str] = None,
    use_scoring_model: bool = False,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    workspace: Optional[Workspace] = None,
) -> tuple[str, str]:
    """Try each configured provider in order. Returns (text, provider_name).

    A workspace with its own OpenRouter key (BYOK) is served by that key
    directly. Non-default workspaces without a key fail explicitly instead of
    silently spending the instance owner's global credentials.

    If *use_scoring_model* is True, automatically picks the cheap/fast model
    instead of the generation model.

    Retries transient errors (429, 5xx) up to _MAX_RETRIES times with
    exponential backoff before falling back to the next provider.
    """
    if workspace is not None:
        if workspace.openrouter_api_key:
            return await _generate_with_workspace_key(
                prompt,
                api_key=workspace.openrouter_api_key,
                model=model
                or _workspace_model(workspace, use_scoring_model=use_scoring_model),
                max_tokens=max_tokens,
                temperature=temperature,
            )
        if workspace.id != DEFAULT_WORKSPACE_ID:
            raise AllProvidersFailedError(
                f"Workspace #{workspace.id} has no OpenRouter API key configured"
            )

    errors: list[str] = []

    for provider in _providers:
        if not provider.is_configured():
            continue

        actual_model = model
        if actual_model is None:
            actual_model = _provider_default_model(
                provider,
                use_scoring_model=use_scoring_model,
            )

        for attempt in range(_MAX_RETRIES + 1):
            try:
                text = await provider.generate(
                    prompt,
                    model=actual_model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                return text, provider.name
            except AIProviderError as e:
                if attempt < _MAX_RETRIES and _is_transient(e):
                    delay = _RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"{provider.name} transient error (attempt {attempt + 1}/{_MAX_RETRIES + 1}), "
                        f"retry in {delay:.0f}s: {e}"
                    )
                    await asyncio.sleep(delay)
                    continue
                errors.append(str(e))
                break

    raise AllProvidersFailedError(
        f"All AI providers failed: {'; '.join(errors) or 'none configured'}"
    )
