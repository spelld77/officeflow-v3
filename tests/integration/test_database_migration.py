from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.session import create_database_engine


def test_initial_migration_creates_expected_tables(tmp_path: Path) -> None:
    database_file = tmp_path / "data" / "officeflow.db"

    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()

    assert {
        "alembic_version",
        "app_settings",
        "attachments",
        "checklist_items",
        "notes",
        "reminders",
        "task_occurrences",
        "tasks",
        "work_logs",
    } <= tables


def test_initial_migration_is_idempotent(tmp_path: Path) -> None:
    database_file = tmp_path / "officeflow.db"

    upgrade_database(database_file)
    upgrade_database(database_file)
