from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text

from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.session import create_database_engine


def test_initial_migration_creates_expected_tables(tmp_path: Path) -> None:
    database_file = tmp_path / "data" / "officeflow.db"

    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    task_indexes = {index["name"] for index in inspector.get_indexes("tasks")}
    with engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
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
    assert {
        "ix_tasks_active_schedule",
        "ix_tasks_completed_at",
        "ix_tasks_deleted_updated",
    } <= task_indexes
    assert revision == "0002_task_query_indexes"


def test_initial_migration_is_idempotent(tmp_path: Path) -> None:
    database_file = tmp_path / "officeflow.db"

    upgrade_database(database_file)
    upgrade_database(database_file)


def test_phase_two_database_receives_task_query_indexes(tmp_path: Path) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    index_names = (
        "ix_tasks_active_schedule",
        "ix_tasks_completed_at",
        "ix_tasks_deleted_updated",
    )
    with engine.begin() as connection:
        for name in index_names:
            connection.exec_driver_sql(f"DROP INDEX {name}")
        connection.exec_driver_sql("UPDATE alembic_version SET version_num = '0001_initial'")
    engine.dispose()

    upgrade_database(database_file)
    migrated_engine = create_database_engine(database_file)
    migrated_indexes = {index["name"] for index in inspect(migrated_engine).get_indexes("tasks")}
    migrated_engine.dispose()

    assert set(index_names) <= migrated_indexes
