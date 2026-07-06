from __future__ import annotations

import re
from pathlib import Path

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "migrations"
SCHEMA_MIGRATIONS_TABLE = "schema_migrations"
BASELINE_SCHEMA_VERSION = "20260628_000000_initial_schema"
_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")
_BASELINE_COLUMNS = {
    "sources": {
        "id",
        "name",
        "url",
        "type",
        "categories",
        "weight",
        "scraper_config",
        "active",
        "added_by",
        "created_at",
        "last_checked_at",
        "last_error",
        "etag",
        "last_modified",
    },
    "news_clusters": {
        "id",
        "topic",
        "topic_original",
        "category",
        "news_type",
        "score",
        "is_hot",
        "status",
        "sources_count",
        "first_seen_at",
        "created_at",
        "updated_at",
    },
    "raw_articles": {
        "id",
        "source_id",
        "external_id",
        "title",
        "content",
        "url",
        "language",
        "published_at",
        "fetched_at",
        "cluster_id",
    },
    "generated_articles": {
        "id",
        "cluster_id",
        "headline",
        "body",
        "summary",
        "ai_provider",
        "version",
        "edit_comment",
        "moderated_by",
        "telegram_message_id",
        "telegram_chat_id",
        "status",
        "created_at",
    },
    "bot_users": {
        "id",
        "username",
        "first_name",
        "role",
        "is_active",
        "created_at",
    },
    "settings": {
        "key",
        "value",
        "updated_at",
    },
}


def split_sql_statements(sql: str) -> list[str]:
    """Split SQL into statements, preserving dollar-quoted blocks."""
    statements: list[str] = []
    current: list[str] = []
    single_quote = False
    dollar_tag: str | None = None
    i = 0

    while i < len(sql):
        char = sql[i]

        if dollar_tag:
            if sql.startswith(dollar_tag, i):
                current.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            current.append(char)
            i += 1
            continue

        if single_quote:
            current.append(char)
            if char == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    current.append("'")
                    i += 2
                    continue
                single_quote = False
            i += 1
            continue

        if sql.startswith("--", i):
            i += 2
            while i < len(sql) and sql[i] != "\n":
                i += 1
            continue

        if sql.startswith("/*", i):
            # PostgreSQL block comments nest; a ';' inside one must not split.
            depth = 1
            i += 2
            while i < len(sql) and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue

        tag = _dollar_tag_at(sql, i)
        if tag:
            dollar_tag = tag
            current.append(tag)
            i += len(tag)
            continue

        if char == "'":
            single_quote = True
            current.append(char)
            i += 1
            continue

        if char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            i += 1
            continue

        current.append(char)
        i += 1

    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


def _dollar_tag_at(sql: str, index: int) -> str | None:
    match = _DOLLAR_TAG_RE.match(sql, index)
    return match.group(0) if match else None


def migration_version(path: Path) -> str:
    return path.stem


def list_migration_files(
    migrations_dir: Path = MIGRATIONS_DIR,
) -> list[Path]:
    if not migrations_dir.exists():
        raise FileNotFoundError(f"Migrations directory not found: {migrations_dir}")

    migrations = sorted(migrations_dir.glob("*.sql"))
    if not migrations:
        raise FileNotFoundError(f"No SQL migrations found in: {migrations_dir}")
    return migrations


async def validate_baseline_schema(conn: object) -> None:
    tables = sorted(_BASELINE_COLUMNS)
    quoted_tables = ", ".join(f"'{table}'" for table in tables)
    result = await conn.execute(
        text(
            f"""
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name IN ({quoted_tables})
            """
        )
    )
    existing = {(row[0], row[1]) for row in result}
    missing = [
        f"{table}.{column}"
        for table, columns in sorted(_BASELINE_COLUMNS.items())
        for column in sorted(columns)
        if (table, column) not in existing
    ]
    if missing:
        raise RuntimeError(
            "Baseline migration did not produce required columns: "
            + ", ".join(missing)
        )


async def list_existing_baseline_tables(conn: object) -> list[str]:
    tables = sorted(_BASELINE_COLUMNS)
    quoted_tables = ", ".join(f"'{table}'" for table in tables)
    result = await conn.execute(
        text(
            f"""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name IN ({quoted_tables})
            """
        )
    )
    return sorted({row[0] for row in result})


async def run_migrations(
    db_engine: AsyncEngine,
    *,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> None:
    """Apply unapplied SQL migrations in filename order."""
    migration_files = list_migration_files(migrations_dir)

    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} (
                    version VARCHAR(255) PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )

        result = await conn.execute(
            text(f"SELECT version FROM {SCHEMA_MIGRATIONS_TABLE}")
        )
        applied_versions = {row[0] for row in result}
        latest_applied_version = max(applied_versions) if applied_versions else None
        if not applied_versions:
            existing_baseline_tables = await list_existing_baseline_tables(conn)
            if existing_baseline_tables:
                raise RuntimeError(
                    "Existing unmanaged application schema detected without "
                    f"{SCHEMA_MIGRATIONS_TABLE} ledger entries: "
                    + ", ".join(existing_baseline_tables)
                )

        for migration_path in migration_files:
            version = migration_version(migration_path)
            if version in applied_versions:
                continue
            if latest_applied_version and version < latest_applied_version:
                raise RuntimeError(
                    "Out-of-order migration detected: "
                    f"{version} is older than already applied "
                    f"{latest_applied_version}"
                )

            statements = split_sql_statements(
                migration_path.read_text(encoding="utf-8")
            )
            for statement in statements:
                await conn.execute(text(statement))

            if version == BASELINE_SCHEMA_VERSION:
                await validate_baseline_schema(conn)

            await conn.execute(
                text(
                    f"INSERT INTO {SCHEMA_MIGRATIONS_TABLE} (version) "
                    "VALUES (:version)"
                ),
                {"version": version},
            )
            logger.info(f"Applied migration: {version}")
            applied_versions.add(version)
            latest_applied_version = max(applied_versions)
