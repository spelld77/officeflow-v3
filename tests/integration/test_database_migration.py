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
    occurrence_indexes = {index["name"] for index in inspector.get_indexes("task_occurrences")}
    delivery_indexes = {
        index["name"] for index in inspector.get_indexes("reminder_deliveries")
    }
    checklist_indexes = {index["name"] for index in inspector.get_indexes("checklist_items")}
    work_log_indexes = {index["name"] for index in inspector.get_indexes("work_logs")}
    with engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    engine.dispose()

    assert {
        "alembic_version",
        "app_settings",
        "attachments",
        "checklist_items",
        "notes",
        "reminder_deliveries",
        "reminders",
        "task_occurrences",
        "tasks",
        "work_logs",
    } <= tables
    assert {
        "ix_tasks_active_schedule",
        "ix_tasks_completed_at",
        "ix_tasks_deleted_updated",
        "ix_tasks_recurrence_window",
    } <= task_indexes
    assert "ix_task_occurrences_window" in occurrence_indexes
    assert {
        "ix_reminder_deliveries_due",
        "ix_reminder_deliveries_schedule",
    } <= delivery_indexes
    assert "ix_checklist_items_task_position" in checklist_indexes
    assert "ix_work_logs_date_updated" in work_log_indexes
    assert revision == "0005_record_indexes"


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


def test_phase_five_database_receives_recurrence_indexes(tmp_path: Path) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)

    assert "ix_tasks_recurrence_window" in {
        index["name"] for index in inspect(engine).get_indexes("tasks")
    }
    assert "ix_task_occurrences_window" in {
        index["name"] for index in inspect(engine).get_indexes("task_occurrences")
    }
    engine.dispose()


def test_phase_five_database_receives_reminder_delivery_history(tmp_path: Path) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    inspector = inspect(engine)

    assert "reminder_deliveries" in inspector.get_table_names()
    assert {
        "ix_reminder_deliveries_due",
        "ix_reminder_deliveries_schedule",
    } <= {index["name"] for index in inspector.get_indexes("reminder_deliveries")}
    engine.dispose()
