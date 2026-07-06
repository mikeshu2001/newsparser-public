from __future__ import annotations

from pathlib import Path

from scripts.safety_check import (
    dedupe_paths,
    find_forbidden_tracked_paths,
    find_secret_findings,
    find_tokenized_remotes,
    has_url_credentials,
    redact_remote_line,
)


def _join(*parts: str) -> str:
    return "".join(parts)


def test_has_url_credentials_detects_tokenized_https_remote() -> None:
    assert has_url_credentials("https://token@example.com/owner/repo.git")
    assert has_url_credentials("https://user:token@example.com/owner/repo.git")
    assert not has_url_credentials("https://github.com/owner/repo.git")
    assert not has_url_credentials("git@github.com:owner/repo.git")


def test_find_tokenized_remotes_ignores_safe_ssh_remote() -> None:
    lines = [
        "origin\thttps://token@github.com/owner/repo.git (fetch)",
        "origin\tgit@github.com:owner/repo.git (push)",
    ]

    assert find_tokenized_remotes(lines) == [
        "origin https://<redacted>@github.com/owner/repo.git (fetch)"
    ]


def test_tokenized_remote_output_redacts_credentials() -> None:
    line = "origin\thttps://user:secret-token@github.com/owner/repo.git (fetch)"

    redacted = redact_remote_line(line)

    assert redacted == "origin https://<redacted>@github.com/owner/repo.git (fetch)"
    assert "secret-token" not in redacted
    assert "user:" not in redacted


def test_find_forbidden_tracked_paths_detects_env_and_venv() -> None:
    assert find_forbidden_tracked_paths([
        ".env",
        ".env.production",
        "deploy/.env.local",
        ".env.example",
        ".venv/bin/python",
        "tools/.venv/bin/python",
        "services/venv/lib/python/site-packages/pkg.py",
        "jobs/env/bin/activate",
        "app/main.py",
    ]) == [
        ".env",
        ".env.production",
        "deploy/.env.local",
        ".venv/bin/python",
        "tools/.venv/bin/python",
        "services/venv/lib/python/site-packages/pkg.py",
        "jobs/env/bin/activate",
    ]


def test_find_secret_findings_detects_realistic_tokens(tmp_path: Path) -> None:
    token_file = tmp_path / "secrets.txt"
    github_token = _join("ghp_", "1234567890abcdefghijklmnopqrstuvwxyzABCD")
    openrouter_token = _join("sk-or-", "1234567890abcdefghijklmnopqrstuvwxyz")
    telegram_token = _join("123456789:", "abcdefghijklmnopqrstuvwxyzABCDE")
    token_file.write_text(
        "\n".join([
            f"github={github_token}",
            f"openrouter={openrouter_token}",
            f"telegram={telegram_token}",
        ]),
        encoding="utf-8",
    )

    findings = find_secret_findings([token_file])

    assert [finding.kind for finding in findings] == [
        "GitHub token",
        "OpenRouter API key",
        "Telegram bot token",
    ]


def test_find_secret_findings_allows_placeholder_examples(tmp_path: Path) -> None:
    example_file = tmp_path / ".env.example"
    example_file.write_text(
        "\n".join([
            "BOT_TOKEN=123456:ABC-DEF...",
            "OPENROUTER_API_KEY=sk-or-...",
        ]),
        encoding="utf-8",
    )

    assert find_secret_findings([example_file]) == []


def test_dedupe_paths_preserves_order() -> None:
    assert dedupe_paths(["a.py", "b.py", "a.py"]) == ["a.py", "b.py"]
