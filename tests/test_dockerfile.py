from __future__ import annotations

from pathlib import Path


def test_dockerfile_copies_migrations_into_runtime_image() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "COPY scripts/migrations/ scripts/migrations/" in dockerfile


def test_production_compose_requires_explicit_postgres_password() -> None:
    base_compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    dev_compose = Path("docker-compose.dev.yml").read_text(encoding="utf-8")

    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}" in base_compose
    assert "ainews_dev_password" not in base_compose
    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-ainews_dev_password}" in dev_compose
