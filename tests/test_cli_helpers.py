from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

from app.database.models import Setting
from scripts import create_migration, update_prompt


class _FixedDatetime(datetime):
    @classmethod
    def now(cls) -> "_FixedDatetime":
        return cls(2026, 6, 28, 12, 34, 56)


def test_create_migration_writes_runner_compatible_template(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(create_migration, "MIGRATIONS_DIR", tmp_path)
    monkeypatch.setattr(create_migration, "datetime", _FixedDatetime)
    monkeypatch.setattr(
        sys,
        "argv",
        ["create_migration.py", "Add Foo Column"],
    )

    create_migration.main()

    migration = tmp_path / "20260628_123456_add_foo_column.sql"
    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert "-- Migration: add_foo_column" in text
    assert "-- Do not include BEGIN/COMMIT" in text
    assert "\nBEGIN;" not in text
    assert "\nCOMMIT;" not in text


def test_create_migration_requires_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["create_migration.py"])

    with pytest.raises(SystemExit) as exc:
        create_migration.main()

    assert exc.value.code == 1


def test_makefile_exposes_local_smoke_targets() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")

    for target in [
        "local-check:",
        "local-bot-start:",
        "local-bot-stop:",
        "local-bot-status:",
        "local-bot-logs:",
        "local-test-draft:",
    ]:
        assert target in makefile

    assert "scripts/local_runtime_check.py --prepare-db --check-ai" in makefile
    assert "scripts/local_runtime_check.py --prepare-db --telegram --check-ai" in makefile
    assert "scripts/create_local_test_draft.py" in makefile


class _FakePromptSession:
    def __init__(self, row: Setting | None):
        self.row = row
        self.added: list[Setting] = []
        self.committed = False

    async def __aenter__(self) -> "_FakePromptSession":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def get(self, model: object, key: str) -> Setting | None:
        assert model is Setting
        assert key == "news_prompt"
        return self.row

    def add(self, row: Setting) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.committed = True


class _FakeEngine:
    def __init__(self):
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


async def test_update_prompt_missing_file_returns_without_db(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.txt"
    session_opened = False

    def async_session() -> _FakePromptSession:
        nonlocal session_opened
        session_opened = True
        return _FakePromptSession(None)

    monkeypatch.setattr(update_prompt, "PROMPT_PATH", missing)
    from app.database import database

    monkeypatch.setattr(database, "async_session", async_session)

    await update_prompt.main()

    assert session_opened is False


async def test_update_prompt_updates_existing_setting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("new prompt\n", encoding="utf-8")
    row = Setting(key="news_prompt", value="old prompt")
    session = _FakePromptSession(row)
    engine = _FakeEngine()

    monkeypatch.setattr(update_prompt, "PROMPT_PATH", prompt_path)
    from app.database import database

    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(database, "engine", engine)

    await update_prompt.main()

    assert row.value == "new prompt"
    assert session.added == []
    assert session.committed is True
    assert engine.disposed is True


async def test_update_prompt_inserts_missing_setting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("inserted prompt", encoding="utf-8")
    session = _FakePromptSession(None)
    engine = _FakeEngine()

    monkeypatch.setattr(update_prompt, "PROMPT_PATH", prompt_path)
    from app.database import database

    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(database, "engine", engine)

    await update_prompt.main()

    assert len(session.added) == 1
    assert session.added[0].key == "news_prompt"
    assert session.added[0].value == "inserted prompt"
    assert session.committed is True
    assert engine.disposed is True
