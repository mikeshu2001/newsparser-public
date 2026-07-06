from __future__ import annotations

from pathlib import Path

import pytest

from app.database.migrations import (
    BASELINE_SCHEMA_VERSION,
    migration_version,
    run_migrations,
    split_sql_statements,
)


def test_split_sql_statements_preserves_dollar_quoted_blocks() -> None:
    sql = """
    -- ignored comment
    ALTER TABLE example ADD COLUMN name TEXT;

    DO $$
    BEGIN
        IF true THEN
            RAISE NOTICE 'hello; world';
        END IF;
    END $$;

    INSERT INTO example (name) VALUES ('a;b');
    """

    statements = split_sql_statements(sql)

    assert len(statements) == 3
    assert statements[0] == "ALTER TABLE example ADD COLUMN name TEXT"
    assert "RAISE NOTICE 'hello; world';" in statements[1]
    assert statements[2] == "INSERT INTO example (name) VALUES ('a;b')"


def test_split_sql_statements_ignores_inline_comments_outside_literals() -> None:
    sql = """
    ALTER TABLE example ADD COLUMN name TEXT; -- add name column
    INSERT INTO example (name) VALUES ('literal -- not a comment');
    DO $$
    BEGIN
        RAISE NOTICE 'block -- not a comment';
    END $$; -- block done
    """

    statements = split_sql_statements(sql)

    assert len(statements) == 3
    assert statements[0] == "ALTER TABLE example ADD COLUMN name TEXT"
    assert statements[1] == (
        "INSERT INTO example (name) VALUES ('literal -- not a comment')"
    )
    assert "block -- not a comment" in statements[2]
    assert "block done" not in statements[2]


def test_migration_version_uses_filename_stem() -> None:
    assert migration_version(Path("20260628_000001_schema_updates.sql")) == (
        "20260628_000001_schema_updates"
    )


def test_list_migration_files_fails_when_directory_missing(tmp_path: Path) -> None:
    from app.database.migrations import list_migration_files

    missing_dir = tmp_path / "missing"

    try:
        list_migration_files(missing_dir)
    except FileNotFoundError as e:
        assert str(missing_dir) in str(e)
    else:
        raise AssertionError("Expected missing migrations directory to fail fast")


def test_list_migration_files_fails_when_directory_has_no_sql(tmp_path: Path) -> None:
    from app.database.migrations import list_migration_files

    (tmp_path / "README.md").write_text("not a migration", encoding="utf-8")

    try:
        list_migration_files(tmp_path)
    except FileNotFoundError as e:
        assert "No SQL migrations" in str(e)
    else:
        raise AssertionError("Expected empty migrations directory to fail fast")


def test_migration_sql_files_parse_without_top_level_transactions() -> None:
    from app.database.migrations import MIGRATIONS_DIR, list_migration_files

    for migration_path in list_migration_files(MIGRATIONS_DIR):
        statements = split_sql_statements(
            migration_path.read_text(encoding="utf-8")
        )

        assert statements, f"{migration_path.name} has no SQL statements"
        for statement in statements:
            first_token = statement.lstrip().split(maxsplit=1)[0].upper()
            assert first_token not in {"BEGIN", "COMMIT"}, (
                f"{migration_path.name} should not manage transactions"
            )


class _Rows:
    def __init__(self, rows: list[tuple[str]] | None = None):
        self._rows = rows or []

    def __iter__(self):
        return iter(self._rows)


def _baseline_column_rows() -> list[tuple[str, str]]:
    from app.database.migrations import _BASELINE_COLUMNS

    return [
        (table, column)
        for table, columns in _BASELINE_COLUMNS.items()
        for column in columns
    ]


class _FakeConnection:
    def __init__(
        self,
        applied_versions: list[str] | None = None,
        existing_tables: list[str] | None = None,
    ):
        self.executed: list[tuple[str, object]] = []
        self.applied_versions = (
            ["001_already_applied"]
            if applied_versions is None
            else applied_versions
        )
        self.existing_tables = existing_tables or []

    async def execute(self, statement: object, params: object = None) -> _Rows:
        sql = str(statement).strip()
        self.executed.append((sql, params))

        if sql == "SELECT version FROM schema_migrations":
            return _Rows([(version,) for version in self.applied_versions])
        if "information_schema.tables" in sql:
            return _Rows([(table,) for table in self.existing_tables])
        if "information_schema.columns" in sql:
            return _Rows(_baseline_column_rows())
        return _Rows()


class _FakeBegin:
    def __init__(self, connection: _FakeConnection):
        self.connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self.connection

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _FakeEngine:
    def __init__(
        self,
        applied_versions: list[str] | None = None,
        existing_tables: list[str] | None = None,
    ):
        self.connection = _FakeConnection(applied_versions, existing_tables)

    def begin(self) -> _FakeBegin:
        return _FakeBegin(self.connection)


async def test_run_migrations_skips_applied_and_records_new_version(tmp_path) -> None:
    (tmp_path / "001_already_applied.sql").write_text(
        "ALTER TABLE skipped ADD COLUMN name TEXT;",
        encoding="utf-8",
    )
    (tmp_path / "002_apply_me.sql").write_text(
        "ALTER TABLE applied ADD COLUMN name TEXT;",
        encoding="utf-8",
    )
    engine = _FakeEngine()

    await run_migrations(engine, migrations_dir=tmp_path)

    executed_sql = [sql for sql, _params in engine.connection.executed]
    insert_params = [
        params
        for sql, params in engine.connection.executed
        if sql.startswith("INSERT INTO schema_migrations")
    ]

    assert not any("ALTER TABLE skipped" in sql for sql in executed_sql)
    assert any("ALTER TABLE applied ADD COLUMN name TEXT" in sql for sql in executed_sql)
    assert insert_params == [{"version": "002_apply_me"}]


async def test_run_migrations_rejects_unapplied_version_older_than_latest_applied(
    tmp_path,
) -> None:
    (tmp_path / "001_backfill.sql").write_text(
        "ALTER TABLE should_not_run ADD COLUMN name TEXT;",
        encoding="utf-8",
    )
    (tmp_path / "002_current.sql").write_text(
        "ALTER TABLE already_applied ADD COLUMN name TEXT;",
        encoding="utf-8",
    )
    engine = _FakeEngine(applied_versions=["002_current"])

    with pytest.raises(RuntimeError, match="Out-of-order migration detected"):
        await run_migrations(engine, migrations_dir=tmp_path)

    executed_sql = [sql for sql, _params in engine.connection.executed]

    assert not any("should_not_run" in sql for sql in executed_sql)
    assert not any(
        sql.startswith("INSERT INTO schema_migrations")
        for sql in executed_sql
    )


async def test_run_migrations_validates_baseline_before_recording_version(
    tmp_path,
) -> None:
    (tmp_path / f"{BASELINE_SCHEMA_VERSION}.sql").write_text(
        "CREATE TABLE IF NOT EXISTS sources (id SERIAL PRIMARY KEY);",
        encoding="utf-8",
    )
    engine = _FakeEngine()
    engine.connection.baseline_rows = [("sources", "id")]

    async def execute_with_incomplete_baseline(
        statement: object,
        params: object = None,
    ) -> _Rows:
        sql = str(statement).strip()
        engine.connection.executed.append((sql, params))
        if sql == "SELECT version FROM schema_migrations":
            return _Rows()
        if "information_schema.tables" in sql:
            return _Rows()
        if "information_schema.columns" in sql:
            return _Rows([("sources", "id")])
        return _Rows()

    engine.connection.execute = execute_with_incomplete_baseline  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Baseline migration did not produce"):
        await run_migrations(engine, migrations_dir=tmp_path)

    assert not any(
        sql.startswith("INSERT INTO schema_migrations")
        for sql, _params in engine.connection.executed
    )


async def test_run_migrations_rejects_unmanaged_existing_schema_before_baseline(
    tmp_path,
) -> None:
    (tmp_path / f"{BASELINE_SCHEMA_VERSION}.sql").write_text(
        "CREATE TABLE IF NOT EXISTS sources (id SERIAL PRIMARY KEY);",
        encoding="utf-8",
    )
    engine = _FakeEngine(applied_versions=[], existing_tables=["sources"])

    with pytest.raises(RuntimeError, match="Existing unmanaged application schema"):
        await run_migrations(engine, migrations_dir=tmp_path)

    executed_sql = [sql for sql, _params in engine.connection.executed]

    assert not any("CREATE TABLE IF NOT EXISTS sources" in sql for sql in executed_sql)
    assert not any(
        sql.startswith("INSERT INTO schema_migrations")
        for sql in executed_sql
    )


class _FailIfOpenedEngine:
    def __init__(self):
        self.begin_called = False

    def begin(self):
        self.begin_called = True
        raise AssertionError("Migration runner opened a DB transaction too early")


async def test_run_migrations_validates_missing_directory_before_transaction(
    tmp_path: Path,
) -> None:
    engine = _FailIfOpenedEngine()
    missing_dir = tmp_path / "missing"

    with pytest.raises(FileNotFoundError):
        await run_migrations(engine, migrations_dir=missing_dir)

    assert engine.begin_called is False


async def test_run_migrations_validates_empty_directory_before_transaction(
    tmp_path: Path,
) -> None:
    engine = _FailIfOpenedEngine()

    with pytest.raises(FileNotFoundError):
        await run_migrations(engine, migrations_dir=tmp_path)

    assert engine.begin_called is False
