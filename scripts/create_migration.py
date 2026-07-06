#!/usr/bin/env python3
"""Generate a timestamped SQL migration file.

Usage:
    python scripts/create_migration.py "add_column_foo_to_bar"

Creates:
    scripts/migrations/YYYYMMDD_HHMMSS_add_column_foo_to_bar.sql

The application migration runner wraps each migration in a transaction, so
generated files should not include BEGIN/COMMIT.
"""

import sys
from datetime import datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/create_migration.py <description>")
        print('Example: python scripts/create_migration.py "add_column_foo_to_bar"')
        sys.exit(1)

    description = sys.argv[1].strip().replace(" ", "_").lower()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{description}.sql"

    MIGRATIONS_DIR.mkdir(parents=True, exist_ok=True)

    filepath = MIGRATIONS_DIR / filename
    filepath.write_text(
        f"-- Migration: {description}\n"
        f"-- Created: {datetime.now().isoformat()}\n\n"
        "-- Write one or more SQL statements ending with semicolons.\n"
        "-- Do not include BEGIN/COMMIT; the runner handles the transaction.\n\n"
        "-- ALTER TABLE ... ADD COLUMN ...;\n",
        encoding="utf-8",
    )

    print(f"Created migration: {filepath}")


if __name__ == "__main__":
    main()
