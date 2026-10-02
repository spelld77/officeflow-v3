from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic import command
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from officeflow.application.attachment_search import AttachmentSearchQuery, AttachmentSearchService
from officeflow.application.attachments import AttachmentService
from officeflow.application.records import RecordService
from officeflow.application.tasks import TaskDraft, TaskService, TaskView
from officeflow.bootstrap.paths import AppPaths
from officeflow.domain.enums import TaskStatus
from officeflow.infrastructure.attachments.storage import ManagedAttachmentStorage
from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.database.attachment_repository import SqlAlchemyAttachmentRepository
from officeflow.infrastructure.database.attachment_search_repository import (
    SqlAlchemyAttachmentSearchRepository,
)
from officeflow.infrastructure.database.migrate import migration_config, upgrade_database
from officeflow.infrastructure.database.models import AppSettingRecord, NoteRecord, TaskRecord
from officeflow.infrastructure.database.record_repository import SqlAlchemyRecordRepository
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository
from officeflow.main import build_application

NOW = datetime(2026, 9, 16, 3, 0, tzinfo=UTC)
TABLES = ("tasks", "work_logs", "notes", "attachments", "app_settings")


@pytest.fixture
def imported_data(tmp_path: Path) -> Iterator[AppPaths]:
    """Existing v3 data with imported provenance, without depending on an importer."""
    paths = AppPaths(tmp_path / "officeflow")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    engine = create_database_engine(paths.database_file)
    sessions = SessionFactory(engine)
    tasks = TaskService(SqlAlchemyTaskRepository(sessions))
    records = RecordService(SqlAlchemyRecordRepository(sessions), tasks)
    attachments = AttachmentService(
        SqlAlchemyAttachmentRepository(sessions),
        ManagedAttachmentStorage(paths.attachment_dir),
        tasks,
    )
    task = tasks.create(TaskDraft(title="이전된 업무", description="원본 내용\n둘째 줄"), now=NOW)
    assert task.id is not None
    records.add_work_log(
        task_id=task.id,
        log_date=date(2026, 9, 16),
        content="기존 업무일지",
        result="처리 결과 보존",
        now=NOW,
    )
    source = tmp_path / "기존보고서.txt"
    source.write_text("이전된 첨부파일", encoding="utf-8")
    attachments.attach(task.id, source, now=NOW)
    with sessions.transaction() as session:
        row = session.get(TaskRecord, task.id)
        assert row is not None
        row.legacy_id = 26
        session.add(NoteRecord(note_date=date(2026, 9, 16), content="기존 메모", updated_at=NOW))
        session.add(
            AppSettingRecord(
                key="legacy_import:" + "a" * 64,
                value_json='{"imported": true}',
                updated_at=NOW,
            )
        )
    engine.dispose()
    yield paths


def _snapshot(paths: AppPaths) -> dict[str, list[tuple[object, ...]]]:
    with closing(sqlite3.connect(paths.database_file)) as connection:
        return {
            table: list(connection.execute(f"SELECT * FROM {table} ORDER BY rowid"))
            for table in TABLES
        }


def _attachment_hashes(paths: AppPaths) -> dict[str, str]:
    return {
        path.relative_to(paths.attachment_dir).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths.attachment_dir.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("older_schema", [False, True])
def test_imported_v3_data_remains_readable_and_editable(
    imported_data: AppPaths, older_schema: bool
) -> None:
    paths = imported_data
    if older_schema:
        command.downgrade(migration_config(paths.database_file), "0009_reminder_schedule_cache")
    before = _snapshot(paths)
    files_before = _attachment_hashes(paths)

    upgrade_database(paths.database_file)
    upgrade_database(paths.database_file)

    assert _snapshot(paths) == before
    assert _attachment_hashes(paths) == files_before
    engine = create_database_engine(paths.database_file)
    try:
        sessions = SessionFactory(engine)
        tasks = TaskService(SqlAlchemyTaskRepository(sessions))
        records = RecordService(SqlAlchemyRecordRepository(sessions), tasks)
        attachments = AttachmentService(
            SqlAlchemyAttachmentRepository(sessions),
            ManagedAttachmentStorage(paths.attachment_dir),
            tasks,
        )
        files = AttachmentSearchService(SqlAlchemyAttachmentSearchRepository(sessions))
        task = tasks.list(TaskView.ALL, search="원본", now=NOW)[0]
        assert task.id is not None
        assert task.legacy_id == 26
        assert records.work_logs(task_id=task.id)[0].content == "기존 업무일지"
        assert records.work_log_page(search="처리 결과").items[0].result == "처리 결과 보존"
        attachment = attachments.attachments_for_task(task.id)[0]
        assert (
            files.search_page(AttachmentSearchQuery(search="기존보고서")).items[0].attachment_id
            == attachment.id
        )
        assert (
            attachments.path_for_open(attachment.id_required).read_text(encoding="utf-8")
            == "이전된 첨부파일"
        )

        updated = tasks.update(
            task.id, TaskDraft(title="이전 업무 수정", description=task.description), now=NOW
        )
        assert updated.legacy_id == 26
        completed = tasks.transition(task.id, TaskStatus.COMPLETED, now=NOW)
        assert completed.legacy_id == 26
        assert completed.status is TaskStatus.COMPLETED
        new_task = tasks.create(TaskDraft(title="새 업무"), now=NOW)
        assert new_task.legacy_id is None
        assert new_task.id != task.id
        assert _attachment_hashes(paths) == files_before
    finally:
        engine.dispose()


@pytest.mark.parametrize("older_schema", [False, True])
def test_imported_v3_backup_and_pending_restore_preserve_all_data(
    imported_data: AppPaths, tmp_path: Path, older_schema: bool
) -> None:
    paths = imported_data
    if older_schema:
        command.downgrade(migration_config(paths.database_file), "0009_reminder_schedule_cache")
    before = _snapshot(paths)
    files_before = _attachment_hashes(paths)
    manager = BackupManager(paths)
    backup = manager.create_backup(reason="manual")
    manifest = manager.verify_backup(backup.path)
    assert manifest.table_counts["tasks"] == 1
    assert manifest.table_counts["work_logs"] == 1
    assert manifest.table_counts["notes"] == 1
    assert len(manifest.attachments) == 1

    target = AppPaths(tmp_path / "restored")
    target.ensure_directories()
    upgrade_database(target.database_file)
    target_manager = BackupManager(target)
    target_manager.stage_restore(backup.path)
    target_manager.apply_pending_restore()
    upgrade_database(target.database_file)

    assert not target.pending_restore_file.exists()
    assert _snapshot(target) == before
    assert _attachment_hashes(target) == files_before
    with closing(sqlite3.connect(target.database_file)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0010_attachment_search"
        )


@pytest.mark.parametrize("pending_restore", [False, True])
def test_application_starts_with_imported_data_without_an_importer(
    imported_data: AppPaths,
    pending_restore: bool,
    monkeypatch: pytest.MonkeyPatch,
    qapp: QApplication,
    qtbot: QtBot,
) -> None:
    paths = imported_data
    before = _snapshot(paths)
    files_before = _attachment_hashes(paths)
    if pending_restore:
        manager = BackupManager(paths)
        backup = manager.create_backup()
        with closing(sqlite3.connect(paths.database_file)) as connection:
            connection.execute("UPDATE tasks SET title = '복원 전 임시 변경'")
            connection.commit()
        manager.stage_restore(backup.path)
    monkeypatch.setenv("OFFICEFLOW_DATA_DIR", str(paths.root))

    app, window = build_application([], application=qapp)
    qtbot.addWidget(window)
    try:
        app.processEvents()
        task = window._task_service.list(TaskView.ALL, search="원본", now=NOW)[0]
        assert task.legacy_id == 26
        assert "2.6" not in window._data_button.toolTip()
        assert not paths.pending_restore_file.exists()
        assert not (paths.root / "migration-reports").exists()
        assert _snapshot(paths) == before
        assert _attachment_hashes(paths) == files_before
    finally:
        window.shutdown()
        window.close()
