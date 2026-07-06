from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.services import ai_providers


class _FakeProvider:
    name = "fake"

    def __init__(
        self,
        *,
        configured: bool = True,
        failures_before_success: int = 0,
        error_text: str = "429 rate_limit",
    ):
        self.configured = configured
        self.failures_before_success = failures_before_success
        self.error_text = error_text
        self.calls: list[dict[str, object]] = []

    def is_configured(self) -> bool:
        return self.configured

    async def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> str:
        self.calls.append({
            "prompt": prompt,
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
        })
        if len(self.calls) <= self.failures_before_success:
            raise ai_providers.AIProviderError(self.error_text)
        return "ok"


def _configure_oauth_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_url: str = "https://oauth-ai.example/v1",
    access_token: str | None = "oauth-token",
    token_file: str | None = None,
    scoring_model: str | None = "oauth-score",
    generation_model: str | None = "oauth-generate",
    token_url: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> None:
    monkeypatch.setattr(ai_providers.settings, "oauth_ai_base_url", base_url)
    monkeypatch.setattr(ai_providers.settings, "oauth_ai_chat_completions_path", "/chat/completions")
    monkeypatch.setattr(ai_providers.settings, "oauth_ai_access_token", access_token)
    monkeypatch.setattr(ai_providers.settings, "oauth_ai_token_file", token_file)
    monkeypatch.setattr(ai_providers.settings, "oauth_ai_scoring_model", scoring_model)
    monkeypatch.setattr(ai_providers.settings, "oauth_ai_generation_model", generation_model)
    monkeypatch.setattr(ai_providers.settings, "oauth_ai_token_url", token_url)
    monkeypatch.setattr(ai_providers.settings, "oauth_ai_client_id", client_id)
    monkeypatch.setattr(ai_providers.settings, "oauth_ai_client_secret", client_secret)
    monkeypatch.setattr(ai_providers.settings, "oauth_ai_refresh_margin_seconds", 60)


async def test_local_test_provider_returns_classification_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ai_providers.settings, "local_ai_provider_enabled", True)
    provider = ai_providers.LocalTestAIProvider()

    response = await provider.generate("Ответь строго в формате JSON: Новость")

    assert provider.is_configured()
    assert json.loads(response) == {
        "type": "other",
        "importance": 5,
        "category": "general",
    }


async def test_local_test_provider_returns_parseable_article_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ai_providers.settings, "local_ai_provider_enabled", True)
    provider = ai_providers.LocalTestAIProvider()

    response = await provider.generate("Сгенерируй новость")

    assert "ЗАГОЛОВОК:" in response
    assert "ТЕКСТ:" in response
    assert "без обращения к внешнему AI API" in response


async def test_codex_cli_provider_runs_codex_exec_and_reads_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ai_providers.settings, "codex_provider_enabled", True)
    monkeypatch.setattr(ai_providers.settings, "codex_bin", "/usr/local/bin/codex")
    monkeypatch.setattr(ai_providers.settings, "codex_model", "gpt-test")
    monkeypatch.setattr(ai_providers.settings, "codex_sandbox", "read-only")
    monkeypatch.setattr(ai_providers.settings, "codex_timeout_seconds", 5)
    seen: dict[str, object] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, stdin: bytes) -> tuple[bytes, bytes]:
            seen["stdin"] = stdin.decode("utf-8")
            output_path = seen["command"][seen["command"].index("--output-last-message") + 1]
            Path(output_path).write_text("codex response\n", encoding="utf-8")
            return b"", b""

    async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> FakeProcess:
        seen["command"] = list(command)
        seen["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(
        ai_providers.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    provider = ai_providers.CodexCLIProvider()

    response = await provider.generate("Original prompt")

    assert response == "codex response"
    assert seen["command"][:2] == ["/usr/local/bin/codex", "exec"]
    assert "--sandbox" in seen["command"]
    assert "read-only" in seen["command"]
    assert "--ephemeral" in seen["command"]
    assert "--skip-git-repo-check" in seen["command"]
    assert "--model" in seen["command"]
    assert "gpt-test" in seen["command"]
    assert seen["command"][-1] == "-"
    assert "Original prompt" in seen["stdin"]
    assert "Return only the requested final answer" in seen["stdin"]


async def test_codex_cli_provider_raises_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ai_providers.settings, "codex_provider_enabled", True)

    class FakeProcess:
        returncode = 1

        async def communicate(self, stdin: bytes) -> tuple[bytes, bytes]:
            return b"", b"not logged in"

    async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(
        ai_providers.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    provider = ai_providers.CodexCLIProvider()

    with pytest.raises(ai_providers.AIProviderError, match="not logged in"):
        await provider.generate("hello")


async def test_codex_cli_provider_times_out_and_kills_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ai_providers.settings, "codex_provider_enabled", True)
    monkeypatch.setattr(ai_providers.settings, "codex_timeout_seconds", 0.01)
    killed = False

    class FakeProcess:
        returncode = None

        async def communicate(self, stdin: bytes) -> tuple[bytes, bytes]:
            await ai_providers.asyncio.sleep(1)
            return b"", b""

        def kill(self) -> None:
            nonlocal killed
            killed = True

        async def wait(self) -> None:
            return None

    async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(
        ai_providers.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    provider = ai_providers.CodexCLIProvider()

    with pytest.raises(ai_providers.AIProviderError, match="timed out"):
        await provider.generate("hello")

    assert killed is True


async def test_oauth_provider_sends_openai_compatible_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_oauth_provider(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer oauth-token"
        assert body == {
            "model": "oauth-generate",
            "messages": [{"role": "user", "content": "write this"}],
            "max_tokens": 123,
            "temperature": 0.4,
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    provider = ai_providers.OAuthAIProvider(transport=httpx.MockTransport(handler))

    text = await provider.generate("write this", max_tokens=123, temperature=0.4)

    assert text == "done"
    assert requests[0].url == "https://oauth-ai.example/v1/chat/completions"


async def test_oauth_provider_reads_access_token_from_json_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "oauth-token.json"
    token_file.write_text(json.dumps({"access_token": "file-token"}), encoding="utf-8")
    _configure_oauth_provider(
        monkeypatch,
        access_token=None,
        token_file=str(token_file),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer file-token"
        return httpx.Response(200, json={"text": "from file"})

    provider = ai_providers.OAuthAIProvider(transport=httpx.MockTransport(handler))

    assert provider.is_configured()
    assert await provider.generate("hello") == "from file"


async def test_oauth_provider_refreshes_expired_token_file_before_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "oauth-token.json"
    token_file.write_text(
        json.dumps({
            "access_token": "old-token",
            "refresh_token": "refresh-token",
            "expires_at": 1,
        }),
        encoding="utf-8",
    )
    _configure_oauth_provider(
        monkeypatch,
        access_token=None,
        token_file=str(token_file),
        token_url="https://oauth-ai.example/oauth/token",
        client_id="client-id",
        client_secret="client-secret",
    )

    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/oauth/token":
            form = parse_qs(request.content.decode("utf-8"))
            assert form["grant_type"] == ["refresh_token"]
            assert form["refresh_token"] == ["refresh-token"]
            assert form["client_id"] == ["client-id"]
            assert form["client_secret"] == ["client-secret"]
            return httpx.Response(200, json={"access_token": "new-token", "expires_in": 3600})

        assert request.headers["Authorization"] == "Bearer new-token"
        return httpx.Response(200, json={"content": "refreshed"})

    provider = ai_providers.OAuthAIProvider(transport=httpx.MockTransport(handler))

    assert await provider.generate("hello") == "refreshed"

    saved_token = json.loads(token_file.read_text(encoding="utf-8"))
    assert seen_paths == ["/oauth/token", "/v1/chat/completions"]
    assert saved_token["access_token"] == "new-token"
    assert saved_token["refresh_token"] == "refresh-token"
    assert saved_token["expires_at"] > time.time()


async def test_generate_falls_back_when_oauth_token_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "oauth-token.json"
    token_file.write_text(
        json.dumps({
            "access_token": "old-token",
            "refresh_token": "refresh-token",
            "expires_at": 1,
        }),
        encoding="utf-8",
    )
    _configure_oauth_provider(
        monkeypatch,
        access_token=None,
        token_file=str(token_file),
        token_url="https://oauth-ai.example/oauth/token",
        client_id="client-id",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    oauth_provider = ai_providers.OAuthAIProvider(transport=httpx.MockTransport(handler))
    fallback_provider = _FakeProvider()
    monkeypatch.setattr(ai_providers, "_providers", [oauth_provider, fallback_provider])

    text, provider_name = await ai_providers.generate("write this")

    assert (text, provider_name) == ("ok", "fake")


async def test_generate_uses_oauth_scoring_model_when_oauth_provider_is_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_oauth_provider(monkeypatch)
    seen_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"output_text": "score"})

    provider = ai_providers.OAuthAIProvider(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ai_providers, "_providers", [provider])

    text, provider_name = await ai_providers.generate(
        "score this",
        use_scoring_model=True,
    )

    assert (text, provider_name) == ("score", "oauth_ai")
    assert seen_models == ["oauth-score"]


async def test_generate_falls_back_to_openrouter_when_oauth_provider_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_oauth_provider(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "expired"})

    oauth_provider = ai_providers.OAuthAIProvider(transport=httpx.MockTransport(handler))
    fallback_provider = _FakeProvider()
    monkeypatch.setattr(ai_providers, "_providers", [oauth_provider, fallback_provider])

    text, provider_name = await ai_providers.generate("write this")

    assert (text, provider_name) == ("ok", "fake")
    assert fallback_provider.calls == [{
        "prompt": "write this",
        "model": None,
        "max_tokens": 4096,
        "temperature": 0.7,
    }]


async def test_generate_uses_scoring_model_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(ai_providers, "_providers", [provider])

    text, provider_name = await ai_providers.generate(
        "score this",
        use_scoring_model=True,
        max_tokens=123,
        temperature=0.0,
    )

    assert (text, provider_name) == ("ok", "fake")
    assert provider.calls == [{
        "prompt": "score this",
        "model": ai_providers.settings.openrouter_scoring_model,
        "max_tokens": 123,
        "temperature": 0.0,
    }]


async def test_generate_leaves_default_generation_model_to_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(ai_providers, "_providers", [provider])

    await ai_providers.generate("write this")

    assert provider.calls[0]["model"] is None


async def test_generate_retries_transient_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider(failures_before_success=1)
    sleeps: list[float] = []
    monkeypatch.setattr(ai_providers, "_providers", [provider])

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(ai_providers.asyncio, "sleep", fake_sleep)

    text, provider_name = await ai_providers.generate("retry me")

    assert (text, provider_name) == ("ok", "fake")
    assert len(provider.calls) == 2
    assert sleeps == [ai_providers._RETRY_BASE_DELAY]


async def test_generate_raises_when_no_provider_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ai_providers, "_providers", [_FakeProvider(configured=False)])

    with pytest.raises(ai_providers.AllProvidersFailedError) as exc:
        await ai_providers.generate("hello")

    assert "none configured" in str(exc.value)


def _workspace(**overrides: object):
    from app.database.models import Workspace

    values: dict[str, object] = {
        "id": 2,
        "name": "Client",
        "openrouter_api_key": "sk-or-client",
    }
    values.update(overrides)
    return Workspace(**values)


async def test_generate_uses_workspace_openrouter_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    seen: dict[str, object] = {}

    class FakeCompletions:
        async def create(self, **kwargs: object) -> SimpleNamespace:
            seen["model"] = kwargs["model"]
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            )

    def fake_client(api_key: str) -> SimpleNamespace:
        seen["api_key"] = api_key
        return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

    monkeypatch.setattr(ai_providers, "_openrouter_client", fake_client)
    monkeypatch.setattr(
        ai_providers.settings, "openrouter_generation_model", "global-gen"
    )

    text, provider = await ai_providers.generate("hi", workspace=_workspace())

    assert (text, provider) == ("ok", "openrouter")
    assert seen["api_key"] == "sk-or-client"
    assert seen["model"] == "global-gen"


async def test_generate_rejects_keyless_non_default_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BYOK contract: client workspaces must not spend the owner's global
    credentials silently."""
    monkeypatch.setattr(ai_providers, "_providers", [_FakeProvider(configured=True)])

    with pytest.raises(
        ai_providers.AllProvidersFailedError, match="no OpenRouter API key"
    ):
        await ai_providers.generate(
            "hi", workspace=_workspace(openrouter_api_key=None)
        )


async def test_generate_default_workspace_without_key_uses_global_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider(configured=True)
    monkeypatch.setattr(ai_providers, "_providers", [provider])

    text, name = await ai_providers.generate(
        "hi", workspace=_workspace(id=1, openrouter_api_key=None)
    )

    assert (text, name) == ("ok", "fake")
    assert provider.calls


async def test_openrouter_provider_rejects_empty_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: None message.content leaked out as generated text and
    crashed the response parser downstream instead of falling through the
    provider chain as AIProviderError."""
    from types import SimpleNamespace

    provider = ai_providers.OpenRouterProvider()

    class FakeCompletions:
        async def create(self, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=None))]
            )

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )

    with pytest.raises(ai_providers.AIProviderError, match="empty content"):
        await provider.generate("prompt", model="test-model")
