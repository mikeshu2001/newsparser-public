from __future__ import annotations

import re

from app.database import database
from app.database.models import Base
from app.database.migrations import MIGRATIONS_DIR, list_migration_files

BASELINE_MIGRATION = MIGRATIONS_DIR / "20260628_000000_initial_schema.sql"
SCHEMA_UPDATES_MIGRATION = MIGRATIONS_DIR / "20260628_000001_schema_updates.sql"


async def test_ensure_schema_runs_migration_runner(monkeypatch) -> None:
    engine = object()
    calls: list[object] = []

    async def fake_run_migrations(db_engine: object) -> None:
        calls.append(db_engine)

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "run_migrations", fake_run_migrations)

    await database.ensure_schema()

    assert calls == [engine]


def test_database_module_has_no_create_all_compatibility_alias() -> None:
    assert "create_all" not in database.__dict__
    assert "Base" not in database.__dict__


def test_baseline_migration_runs_before_schema_updates() -> None:
    names = [path.name for path in list_migration_files(MIGRATIONS_DIR)]

    assert names.index("20260628_000000_initial_schema.sql") < names.index(
        "20260628_000001_schema_updates.sql"
    )


def _later_migration_sql() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in list_migration_files(MIGRATIONS_DIR)
        if path.name != BASELINE_MIGRATION.name
    )


def _columns_added_by_later_migrations() -> set[tuple[str, str]]:
    """ORM columns introduced by post-baseline migrations via ALTER TABLE."""
    return {
        (match.group(1).lower(), match.group(2).lower())
        for match in re.finditer(
            r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
            _later_migration_sql(),
            flags=re.IGNORECASE,
        )
    }


def _tables_created_by_later_migrations() -> set[str]:
    return {
        match.group(1).lower()
        for match in re.finditer(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
            _later_migration_sql(),
            flags=re.IGNORECASE,
        )
    }


def _indexes_created_by_later_migrations() -> set[str]:
    return {
        match.group(1).lower()
        for match in re.finditer(
            r"CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
            _later_migration_sql(),
            flags=re.IGNORECASE,
        )
    }


def test_baseline_migration_covers_orm_tables_and_columns() -> None:
    sql = BASELINE_MIGRATION.read_text(encoding="utf-8")
    added_later = _columns_added_by_later_migrations()
    tables_later = _tables_created_by_later_migrations()

    for table in Base.metadata.sorted_tables:
        if table.name.lower() in tables_later:
            continue
        table_block = _extract_create_table_block(sql, table.name)

        for column in table.columns:
            if (table.name.lower(), column.name.lower()) in added_later:
                continue
            assert re.search(
                rf"^\s+{re.escape(column.name)}\s+",
                table_block,
                flags=re.IGNORECASE | re.MULTILINE,
            ), f"Missing column {table.name}.{column.name} in baseline migration"


def test_baseline_migration_covers_orm_indexes() -> None:
    sql = BASELINE_MIGRATION.read_text(encoding="utf-8")
    tables_later = _tables_created_by_later_migrations()
    indexes_later = _indexes_created_by_later_migrations()

    for table in Base.metadata.sorted_tables:
        if table.name.lower() in tables_later:
            continue
        for index in table.indexes:
            if index.name.lower() in indexes_later:
                continue
            assert re.search(
                rf"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+{re.escape(index.name)}\s+ON\s+{re.escape(table.name)}\s*\(",
                sql,
                flags=re.IGNORECASE,
            ), f"Missing index {index.name} in baseline migration"


def test_bot_user_id_is_manual_telegram_id_contract() -> None:
    bot_users_block = _extract_create_table_block(
        BASELINE_MIGRATION.read_text(encoding="utf-8"),
        "bot_users",
    )
    bot_user_id = Base.metadata.tables["bot_users"].c.id

    assert bot_user_id.autoincrement is False
    id_line_match = re.search(
        r"^\s+id\s+BIGINT\s+PRIMARY\s+KEY\b",
        bot_users_block,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    assert id_line_match is not None
    id_line = id_line_match.group(0)
    assert "DEFAULT" not in id_line.upper()
    assert "SERIAL" not in id_line.upper()
    assert "NEXTVAL" not in id_line.upper()


def test_schema_updates_scope_information_schema_checks_to_current_schema() -> None:
    sql = SCHEMA_UPDATES_MIGRATION.read_text(encoding="utf-8")
    checks = re.findall(
        r"SELECT\s+1\s+FROM\s+information_schema\.columns(.*?)\)\s+THEN",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )

    assert checks
    for check in checks:
        assert "table_schema = current_schema()" in check


def test_schema_updates_drop_bot_users_id_default() -> None:
    sql = SCHEMA_UPDATES_MIGRATION.read_text(encoding="utf-8")

    assert re.search(
        r"ALTER\s+TABLE\s+bot_users\s+ALTER\s+COLUMN\s+id\s+DROP\s+DEFAULT\s*;",
        sql,
        flags=re.IGNORECASE,
    )


def _extract_create_table_block(sql: str, table_name: str) -> str:
    match = re.search(
        rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table_name)}\s*\((.*?)\n\);",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )

    assert match is not None, f"Missing table {table_name} in baseline migration"
    return match.group(1)
