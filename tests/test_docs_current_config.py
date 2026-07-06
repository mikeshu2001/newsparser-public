"""Contract tests keeping the public docs in sync with the runtime config."""

from __future__ import annotations

from pathlib import Path

DEPLOYMENT = Path("docs/DEPLOYMENT.md")
ENV_EXAMPLE = Path(".env.example")
README = Path("README.md")

_OBSOLETE_ENV_NAMES = (
    "CLAUDE_API_KEY",
    "CLAUDE_SCORING_MODEL",
    "CLAUDE_GENERATION_MODEL",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "GLM_API_KEY",
)


def test_deployment_docs_pin_migration_only_contract() -> None:
    deployment = DEPLOYMENT.read_text(encoding="utf-8")

    assert "scripts/migrations" in deployment
    assert "create_all()" in deployment  # documented as removed
    assert "schema_migrations" in deployment


def test_deployment_runbook_documents_db_rollback_policy() -> None:
    deployment = DEPLOYMENT.read_text(encoding="utf-8").lower()

    assert "rollback" in deployment
    assert "migration boundary" in deployment


def test_env_example_uses_current_openrouter_names() -> None:
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "OPENROUTER_API_KEY" in env_example
    assert "OPENROUTER_SCORING_MODEL" in env_example
    assert "OPENROUTER_GENERATION_MODEL" in env_example


def test_public_docs_do_not_mention_obsolete_provider_env_names() -> None:
    for path in (DEPLOYMENT, ENV_EXAMPLE, README):
        text = path.read_text(encoding="utf-8")
        for name in _OBSOLETE_ENV_NAMES:
            assert name not in text, f"{path}: obsolete env name {name}"
