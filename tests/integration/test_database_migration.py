from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import inspect, text

from officeflow.application.records import RecordService
from officeflow.application.tasks import TaskDraft, TaskQuery, TaskService, TaskView
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.record_repository import SqlAlchemyRecordRepository
from officeflow.infrastructure.database.session import (
    SessionFactory,
    create_database_engine,
)
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository


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
    attachment_indexes = {index["name"] for index in inspector.get_indexes("attachments")}
    attachment_columns = {column["name"] for column in inspector.get_columns("attachments")}
    with engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        triggers = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
        }
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
        "task_search",
        "work_log_search",
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
    assert {"ix_attachments_task_missing", "ix_attachments_detached_created"} <= attachment_indexes
    assert "detached_at" in attachment_columns
    assert {
        "task_search_insert",
        "task_search_update",
        "task_search_delete",
        "work_log_search_insert",
        "work_log_search_update",
        "work_log_search_delete",
    } <= triggers
    assert revision == "0008_full_text_search"


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


def test_existing_database_receives_attachment_cleanup_state(tmp_path: Path) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ix_attachments_detached_created")
        connection.exec_driver_sql("ALTER TABLE attachments DROP COLUMN detached_at")
        connection.exec_driver_sql(
            "UPDATE alembic_version SET version_num = '0006_attachment_index'"
        )
    engine.dispose()

    upgrade_database(database_file)
    migrated_engine = create_database_engine(database_file)
    inspector = inspect(migrated_engine)

    assert "detached_at" in {
        column["name"] for column in inspector.get_columns("attachments")
    }
    assert "ix_attachments_detached_created" in {
        index["name"] for index in inspector.get_indexes("attachments")
    }
    migrated_engine.dispose()


def test_existing_records_are_backfilled_into_full_text_indexes(tmp_path: Path) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    sessions = SessionFactory(engine)
    task_service = TaskService(SqlAlchemyTaskRepository(sessions))
    record_service = RecordService(SqlAlchemyRecordRepository(sessions), task_service)
    task = task_service.create(TaskDraft(title="기존 계약 검색"))
    assert task.id is not None
    record_service.add_work_log(
        task_id=task.id,
        log_date=date(2026, 9, 20),
        content="이전 업무일지 본문",
    )
    with engine.begin() as connection:
        for trigger in (
            "work_log_search_delete",
            "work_log_search_update",
            "work_log_search_insert",
            "task_search_delete",
            "task_search_update",
            "task_search_insert",
        ):
            connection.exec_driver_sql(f"DROP TRIGGER {trigger}")
        connection.exec_driver_sql("DROP TABLE work_log_search")
        connection.exec_driver_sql("DROP TABLE task_search")
        connection.exec_driver_sql(
            "UPDATE alembic_version SET version_num = '0007_attachment_cleanup'"
        )
    engine.dispose()

    upgrade_database(database_file)
    migrated_engine = create_database_engine(database_file)
    migrated_sessions = SessionFactory(migrated_engine)
    migrated_tasks = TaskService(SqlAlchemyTaskRepository(migrated_sessions))
    migrated_records = RecordService(
        SqlAlchemyRecordRepository(migrated_sessions), migrated_tasks
    )

    assert migrated_tasks.query(
        TaskQuery(view=TaskView.ALL, search="기존 계약")
    ).total == 1
    assert migrated_records.work_log_page(search="이전 업무일지").total == 1
    migrated_engine.dispose()
