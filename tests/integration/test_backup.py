from __future__ import annotations

import os
import sqlite3
import zipfile
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from officeflow.application.tasks import TaskDraft, TaskService
from officeflow.bootstrap.paths import AppPaths
from officeflow.domain.enums import TaskStatus
from officeflow.infrastructure.backup import BackupError, BackupManager
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.models import AttachmentRecord
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository


def _database(path, values: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("DELETE FROM sample")
        connection.executemany("INSERT INTO sample(value) VALUES (?)", ((value,) for value in values))
        connection.commit()


def _values(path) -> tuple[str, ...]:
    with closing(sqlite3.connect(path)) as connection:
        return tuple(row[0] for row in connection.execute("SELECT value FROM sample ORDER BY id"))


def test_online_backup_verifies_and_restores_database_attachments_and_settings(tmp_path) -> None:
    paths = AppPaths(tmp_path / "officeflow")
    paths.ensure_directories()
    _database(paths.database_file, ("before",))
    attachment = paths.attachment_dir / "ab" / "original.bin"
    attachment.parent.mkdir()
    attachment.write_bytes(b"original attachment")
    paths.settings_file.write_text('{"theme": "light"}', encoding="utf-8")
    manager = BackupManager(paths)

    created = manager.create_backup(reason="manual")
    manifest = manager.verify_backup(created.path)
    assert manifest.table_counts["sample"] == 1
    assert len(manifest.attachments) == 1

    _database(paths.database_file, ("changed", "second"))
    attachment.write_bytes(b"changed")
    paths.settings_file.write_text('{"theme": "dark"}', encoding="utf-8")
    manager.stage_restore(created.path)
    pre_restore = manager.apply_pending_restore()

    assert pre_restore is not None
    assert pre_restore.reason == "pre-restore"
    assert _values(paths.database_file) == ("before",)
    assert attachment.read_bytes() == b"original attachment"
    assert paths.settings_file.read_text(encoding="utf-8") == '{"theme": "light"}'
    assert not paths.pending_restore_file.exists()
    assert manager.verify_backup(pre_restore.path).table_counts["sample"] == 2


def test_backup_verification_rejects_tampered_attachment(tmp_path) -> None:
    paths = AppPaths(tmp_path / "officeflow")
    paths.ensure_directories()
    _database(paths.database_file, ("one",))
    attachment = paths.attachment_dir / "ab" / "file.bin"
    attachment.parent.mkdir()
    attachment.write_bytes(b"safe")
    manager = BackupManager(paths)
    created = manager.create_backup()
    tampered = paths.backup_dir / "tampered.ofbackup"

    with zipfile.ZipFile(created.path, "r") as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename.startswith("attachments/"):
                data = b"tampered"
            target.writestr(info, data)

    with pytest.raises(BackupError, match=r"크기|체크섬"):
        manager.verify_backup(tampered)


def test_automatic_backup_retention_removes_only_old_automatic_files(tmp_path) -> None:
    paths = AppPaths(tmp_path / "officeflow")
    paths.ensure_directories()
    _database(paths.database_file, ("one",))
    manager = BackupManager(paths)
    automatic = [manager.create_backup(reason="automatic").path for _ in range(3)]
    manual = manager.create_backup(reason="manual").path
    for index, path in enumerate(automatic):
        os.utime(path, (100 + index, 100 + index))

    removed = manager.prune_automatic_backups(2)

    assert removed == (automatic[0],)
    assert manual.exists()
    assert not automatic[0].exists()
    assert automatic[1].exists() and automatic[2].exists()


def test_data_usage_reports_records_files_and_cleanup_candidates(tmp_path) -> None:
    paths = AppPaths(tmp_path / "officeflow")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    engine = create_database_engine(paths.database_file)
    sessions = SessionFactory(engine)
    task_service = TaskService(SqlAlchemyTaskRepository(sessions))
    now = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
    active = task_service.create(TaskDraft(title="진행 업무"), now=now)
    task_service.create(
        TaskDraft(title="완료 업무", status=TaskStatus.COMPLETED),
        now=now,
    )
    deleted = task_service.create(TaskDraft(title="오래된 삭제 업무"), now=now)
    assert active.id is not None and deleted.id is not None
    task_service.move_to_trash(deleted.id, now=now - timedelta(days=31))

    linked_path = paths.attachment_dir / "aa" / "linked.bin"
    linked_path.parent.mkdir()
    linked_path.write_bytes(b"linked")
    with sessions.transaction() as session:
        session.add_all(
            (
                AttachmentRecord(
                    task_id=active.id,
                    original_name="연결파일.bin",
                    stored_name="linked.bin",
                    relative_path="aa/linked.bin",
                    size_bytes=6,
                    checksum=None,
                    created_at=now,
                    missing_at=None,
                ),
                AttachmentRecord(
                    task_id=active.id,
                    original_name="누락파일.bin",
                    stored_name="missing.bin",
                    relative_path="aa/missing.bin",
                    size_bytes=100,
                    checksum=None,
                    created_at=now,
                    missing_at=None,
                ),
            )
        )
    orphan_path = paths.attachment_dir / "bb" / "orphan.bin"
    orphan_path.parent.mkdir()
    orphan_path.write_bytes(b"orphaned-data")
    (paths.backup_dir / "one.ofbackup").write_bytes(b"backup-one")
    (paths.backup_dir / "two.ofbackup").write_bytes(b"backup-two")

    usage = BackupManager(paths).inspect_data_usage(now=now)

    assert usage.task_count == 3
    assert usage.open_task_count == 1
    assert usage.completed_task_count == 1
    assert usage.trash_task_count == 1
    assert usage.aged_trash_task_count == 1
    assert usage.linked_attachment_count == 2
    assert usage.stored_attachment_count == 2
    assert usage.missing_attachment_count == 1
    assert usage.orphan_attachment_count == 1
    assert usage.orphan_attachment_bytes == len(b"orphaned-data")
    assert usage.backup_count == 2
    assert usage.backup_bytes == len(b"backup-onebackup-two")
    assert usage.database_bytes > 0
    assert usage.largest_files[0].orphaned is True
    assert usage.total_bytes == (
        usage.database_bytes + usage.stored_attachment_bytes + usage.backup_bytes
    )
    engine.dispose()
