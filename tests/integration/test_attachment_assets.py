"""Phase 2 foundation: preservation, all references, metadata and complete backups."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from threading import Event, Thread
from uuid import uuid4

import pytest
from sqlalchemy import Engine, event, text
from sqlalchemy.exc import IntegrityError

from officeflow.application.attachment_export import (
    AttachmentExportService,
    ExportStatus,
    FileReference,
)
from officeflow.application.attachment_search import AttachmentSearchQuery, AttachmentSearchService
from officeflow.application.attachments import AttachmentOperationError
from officeflow.application.tasks import TaskDraft
from officeflow.bootstrap.paths import AppPaths
from officeflow.domain.attachment import AttachmentRole
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
from officeflow.infrastructure.backup import BackupError, BackupManager
from officeflow.infrastructure.database.attachment_search_repository import (
    SqlAlchemyAttachmentSearchRepository,
)
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.models import (
    AttachmentRecord,
    AttachmentVersionRecord,
    FileAssetRecord,
)
from tests.integration.legacy_attachment_fixture import legacy_environment
from tests.integration.test_attachment_search import search_environment


def snapshot(database):
    with closing(sqlite3.connect(database)) as connection:
        return tuple(connection.iterdump())


def add_file(env, task_id, name, content=b"fixture bytes"):
    source = env.database.parent / name
    source.write_bytes(content)
    return env.attachments.attach(task_id, source)


@pytest.mark.parametrize(
    "failure", [None, "UPDATE attachments SET asset_id", "CREATE TRIGGER file_asset_immutable"]
)
def test_genuine_0012_migration_preserves_fields_files_and_rolls_back(tmp_path, failure):
    with legacy_environment(tmp_path / "legacy") as env:
        task = env.tasks.create(TaskDraft(title="기존 자료"))
        active = env.add(task.id, "기존.txt")
        detached = env.add(task.id, "해제.txt", detached_at=datetime(2026, 10, 2, tzinfo=UTC))
        missing = env.add(task.id, "누락.txt")
        missing.path.unlink()
        active.path.write_bytes(b"saved after import")
        with env.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE attachments SET observed_size_bytes=18, observed_mtime_ns=123, observed_checksum=:checksum, observed_at='2026-10-02 00:00:00',last_checked_at='2026-10-02 00:00:00',last_check_status='modified' WHERE id=:id"
                ),
                {"id": active.id, "checksum": hashlib.sha256(b"saved after import").hexdigest()},
            )
        database = env.database
        before = snapshot(database)
        physical = {
            active.path: active.path.read_bytes(),
            detached.path: detached.path.read_bytes(),
        }
    if failure:

        def fail(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.startswith(failure):
                raise sqlite3.OperationalError("synthetic migration failure")

        event.listen(Engine, "before_cursor_execute", fail)
        try:
            with pytest.raises(Exception, match="synthetic migration"):
                upgrade_database(database)
        finally:
            event.remove(Engine, "before_cursor_execute", fail)
        assert snapshot(database) == before
    upgrade_database(database)
    with search_environment(database.parent) as migrated:
        items = [
            migrated.repository.get_attachment(item.id) for item in (active, detached, missing)
        ]
        assert all(item.role is AttachmentRole.GENERAL for item in items)
        assert (
            len({item.asset_id for item in items}) == len({item.family_id for item in items}) == 3
        )
        assert items[0].name == items[0].original_name == "기존.txt"
        assert items[0].relative_path == active.relative_path
        assert items[0].checksum == active.checksum
        assert items[0].observed_size_bytes == 18
        assert items[1].detached_at is not None
        assert migrated.attachments.path_for_open(active.id).read_bytes() == b"saved after import"
        assert migrated.repository.get_attachment(missing.id) is not None
    assert all(path.read_bytes() == content for path, content in physical.items())
    assert not missing.path.exists()
    backups = list((database.parent / "schema-backups").glob("pre-attachment-assets-*.db"))
    assert backups and snapshot(backups[0]) == before
    with closing(sqlite3.connect(database)) as connection:
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
        assert "relative_path" not in {
            row[1] for row in connection.execute("PRAGMA table_info(attachments)")
        }


@pytest.mark.parametrize(
    "boundary",
    [
        "CREATE TABLE document_families",
        "ALTER TABLE _phase2_attachments RENAME",
        "CREATE TRIGGER file_asset_immutable",
    ],
)
def test_actual_process_exit_during_migration_is_atomic_and_retryable(tmp_path, boundary):
    with legacy_environment(tmp_path / "crash") as env:
        task = env.tasks.create(TaskDraft(title="중단 복구"))
        attachment = env.add(task.id, "보존.txt")
        database = env.database
    before = snapshot(database)
    code = """
import os, sys
from pathlib import Path
from sqlalchemy import Engine, event
from officeflow.infrastructure.database.migrate import upgrade_database
def abrupt(conn, cursor, statement, parameters, context, many):
    if statement.lstrip().startswith(sys.argv[2]):
        os._exit(73)
event.listen(Engine, 'before_cursor_execute', abrupt)
upgrade_database(Path(sys.argv[1]))
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(database), boundary], capture_output=True, timeout=30
    )
    assert process.returncode == 73, process.stderr.decode(errors="replace")
    assert snapshot(database) == before
    upgrade_database(database)
    with search_environment(database.parent) as reopened:
        assert (
            reopened.attachments.path_for_open(attachment.id).read_bytes()
            == b"legacy saved content"
        )


def test_migration_uses_indexed_map_and_staging_self_reference(tmp_path):
    with legacy_environment(tmp_path / "legacy") as env:
        task = env.tasks.create(TaskDraft(title="색인 검증"))
        env.add(task.id, "유지.txt")
        database = env.database
    plans, self_references = [], []

    def inspect_plan(connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE attachments SET asset_id"):
            plans.extend(
                str(row[-1])
                for row in connection.exec_driver_sql(
                    "EXPLAIN QUERY PLAN SELECT asset_id FROM attachment_asset_map WHERE id=1"
                )
            )
        if statement.strip() == "DROP TABLE attachments":
            self_references.extend(
                row[2]
                for row in connection.exec_driver_sql(
                    "PRAGMA foreign_key_list(_phase2_attachments)"
                )
                if row[3] == "source_attachment_id"
            )

    event.listen(Engine, "before_cursor_execute", inspect_plan)
    try:
        upgrade_database(database)
    finally:
        event.remove(Engine, "before_cursor_execute", inspect_plan)
    assert any("SEARCH" in plan and "attachment_asset_map_id" in plan for plan in plans)
    assert self_references == ["_phase2_attachments"]


def frozen_fixture(env, attachment):
    """Only a test fixture; public original designation is not enabled yet."""
    token = uuid4().hex
    relative = f"frozen/{token[:2]}/{token}.txt"
    path = env.attachments._storage.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"frozen bytes")
    with env.sessions.transaction() as session:
        row = session.get(AttachmentRecord, attachment.id)
        asset = FileAssetRecord(
            kind="frozen",
            state="active",
            stored_name=path.name,
            relative_path=relative,
            fixed_size_bytes=path.stat().st_size,
            fixed_checksum=hashlib.sha256(path.read_bytes()).hexdigest(),
            frozen_at=datetime.now(UTC),
        )
        session.add(asset)
        session.flush()
        row.asset = asset
        row.role = "original"
        session.flush()
        version = AttachmentVersionRecord(
            task_id=row.task_id,
            family_id=row.family_id,
            asset_id=asset.id,
            label="검토 반영",
            suggestion_number=1,
            original_name=row.display_name,
            memo="보관 설명",
            created_at=datetime.now(UTC),
            source_attachment_id=row.id,
            source_payload=json.dumps({"name": row.display_name}),
        )
        session.add(version)
        session.flush()
        version_id = version.id
    return env.repository.get_attachment(attachment.id), version_id, path


def test_frozen_asset_constraints_and_reference_safe_deletion(tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="고정 보관"))
        attached = add_file(env, task.id, "서식.txt")
        original, version_id, path = frozen_fixture(env, attached)
        with pytest.raises(AttachmentOperationError, match="직접 열"):
            env.attachments.path_for_open(original.id)
        for update_sql in (
            "UPDATE file_assets SET fixed_checksum='" + "a" * 64 + "' WHERE id=:id",
            "UPDATE file_assets SET relative_path='changed.txt' WHERE id=:id",
        ):
            with pytest.raises(IntegrityError, match="immutable"), env.engine.begin() as connection:
                connection.execute(text(update_sql), {"id": original.asset_id})
        env.attachments.delete_file(original.id)
        assert path.read_bytes() == b"frozen bytes"
        with env.sessions.transaction() as session:
            version = session.get(AttachmentVersionRecord, version_id)
            assert version.asset_id == original.asset_id
            assert version.source_attachment_id is None
            assert json.loads(version.source_payload)["name"] == "서식.txt"
        assert original.relative_path in env.repository.paths_for_task(task.id)
        env.tasks.move_to_trash(task.id)
        paths = env.tasks.delete_permanently(task.id)
        assert env.attachments.remove_files_after_task_delete(paths) == ()
        assert not path.exists()


def test_rename_does_not_rewrite_version_snapshot_and_changed_frozen_is_not_exported(tmp_path):
    with search_environment(tmp_path / "profile") as env:
        task = env.tasks.create(TaskDraft(title="이전 보관 정보"))
        attached = add_file(env, task.id, "초기서식.txt")
        original, version_id, path = frozen_fixture(env, attached)
        env.attachments.rename_attachment(original.id, "현재서식.txt", expected_revision=0)
        with env.sessions.transaction() as session:
            version = session.get(AttachmentVersionRecord, version_id)
            assert version.original_name == "초기서식.txt" and version.label == "검토 반영"
            assert json.loads(version.source_payload)["name"] == "초기서식.txt"
        with pytest.raises(IntegrityError, match="immutable"), env.engine.begin() as connection:
            connection.execute(
                text("UPDATE attachment_versions SET source_payload='{}' WHERE id=:id"),
                {"id": version_id},
            )
        with pytest.raises(IntegrityError, match="immutable"), env.engine.begin() as connection:
            connection.execute(
                text("UPDATE attachments SET original_name='덮어쓰기.txt' WHERE id=:id"),
                {"id": original.id},
            )
        folder = tmp_path / "exports"
        folder.mkdir()
        exporter = AttachmentExportService(
            env.attachments, ExternalExportStorage(env.database.parent)
        )
        path.write_bytes(b"changed fixed source")
        result = exporter.export((FileReference.attachment(original.id),), folder)
        assert result.items[0].status is ExportStatus.FAILED
        assert "보관 후 내용이 변경" in result.items[0].message
        assert not list(folder.iterdir())
        assert env.repository.get_attachment(original.id).fixed_checksum == original.fixed_checksum


@pytest.mark.parametrize(
    "invalid",
    [
        "UPDATE attachments SET role='original' WHERE id=:id",
        "INSERT INTO file_assets(id,kind,state,stored_name,relative_path,created_at) VALUES('bad','frozen','active','bad.txt','bad.txt','2026-10-01')",
    ],
)
def test_invalid_asset_role_and_missing_fixed_values_are_rejected(tmp_path, invalid):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="제약"))
        attachment = env.add(task.id, "일반.txt")
        with pytest.raises(IntegrityError), env.engine.begin() as connection:
            connection.execute(text(invalid), {"id": attachment.id})
        assert env.repository.get_attachment(attachment.id).role is AttachmentRole.GENERAL


def test_unknown_future_asset_reference_fails_closed_for_delete_and_backup(tmp_path):
    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        task = env.tasks.create(TaskDraft(title="미래 보존 연결"))
        attachment = add_file(env, task.id, "유지.txt")
        with env.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE future_snapshot(id INTEGER PRIMARY KEY,asset_id VARCHAR(32) REFERENCES file_assets(id))"
            )
            connection.execute(
                text("INSERT INTO future_snapshot(asset_id) VALUES (:id)"),
                {"id": attachment.asset_id},
            )
        before = (paths.attachment_dir / attachment.relative_path).read_bytes()
        with pytest.raises(ValueError, match="지원하지 않는 파일 보존 참조"):
            env.attachments.delete_file(attachment.id)
        with pytest.raises(BackupError, match="지원하지 않는 파일 보존 참조"):
            BackupManager(paths).create_backup()
        env.tasks.move_to_trash(task.id)
        with pytest.raises(ValueError, match="지원하지 않는 파일 보존 참조"):
            env.tasks.delete_permanently(task.id)
        assert env.repository.get_attachment(attachment.id) is not None
        assert (paths.attachment_dir / attachment.relative_path).read_bytes() == before


def test_rename_memo_search_export_and_cas_keep_initial_name_path_and_bytes(tmp_path):
    with search_environment(tmp_path / "profile") as env:
        task = env.tasks.create(TaskDraft(title="이름 변경"))
        attachment = add_file(env, task.id, "옛이름.txt")
        seen = []
        env.attachments.subscribe(seen.append)
        renamed = env.attachments.rename_attachment(
            attachment.id, "현재보고서.txt", expected_revision=0
        )
        assert renamed.original_name == "옛이름.txt" and renamed.name == "현재보고서.txt"
        assert (renamed.asset_id, renamed.family_id, renamed.relative_path, renamed.checksum) == (
            attachment.asset_id,
            attachment.family_id,
            attachment.relative_path,
            attachment.checksum,
        )
        with pytest.raises(ValueError, match="첨부 정보가 변경"):
            env.attachments.update_memo(attachment.id, "오래된 화면", expected_revision=0)
        saved = env.attachments.update_memo(
            attachment.id, "파일별 설명\n업무일지 아님", expected_revision=1
        )
        assert saved.metadata_revision == 2
        search = AttachmentSearchService(SqlAlchemyAttachmentSearchRepository(env.sessions))
        assert len(search.search_page(AttachmentSearchQuery(search="현재보고서")).items) == 1
        assert not search.search_page(AttachmentSearchQuery(search="옛이름")).items
        folder = tmp_path / "exported"
        folder.mkdir()
        export = AttachmentExportService(
            env.attachments, ExternalExportStorage(tmp_path / "profile")
        )
        result = export.export((FileReference.attachment(attachment.id),), folder)
        assert result.items[0].status is ExportStatus.SAVED
        assert result.items[0].output_path.name == "현재보고서.txt"
        assert (
            env.attachments.path_for_open(attachment.id).read_bytes()
            == result.items[0].output_path.read_bytes()
        )
        assert seen == [task.id, task.id, task.id]  # rename, memo, content observation at export
        env.attachments.unlink(attachment.id)
        with pytest.raises(ValueError, match="복원"):
            env.attachments.rename_attachment(attachment.id, "금지.txt", expected_revision=3)


def test_format3_backup_includes_version_only_asset_and_rejects_changed_frozen(tmp_path):
    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        task = env.tasks.create(TaskDraft(title="전체 백업"))
        attached = add_file(env, task.id, "서식.txt")
        original, version_id, path = frozen_fixture(env, attached)
        env.attachments.delete_file(original.id)
        manager = BackupManager(paths)
        backup = manager.create_backup()
        manifest = manager.verify_backup(backup.path)
        assert manifest.format_version == 3
        assert any(
            item.asset_id == original.asset_id and item.category == "asset"
            for item in manifest.attachments
        )
        target = AppPaths(tmp_path / "restored")
        target.ensure_directories()
        upgrade_database(target.database_file)
        restore = BackupManager(target)
        restore.stage_restore(backup.path)
        restore.apply_pending_restore()
        with search_environment(target.data_dir, attachment_root=target.attachment_dir) as reopened:
            with reopened.sessions.transaction() as session:
                version = session.get(AttachmentVersionRecord, version_id)
                assert version.memo == "보관 설명"
                assert version.asset.fixed_checksum == original.fixed_checksum
            assert (target.attachment_dir / original.relative_path).read_bytes() == b"frozen bytes"
        path.write_bytes(b"outside alteration")
        with pytest.raises(BackupError, match="보관 후 내용이 변경"):
            manager.create_backup()


def test_normal_startup_without_pending_trash_does_not_query_all_paths(tmp_path, monkeypatch):
    manager = BackupManager(AppPaths(tmp_path / "profile"))

    def forbidden(*args, **kwargs):
        raise AssertionError("normal startup must not read the DB/file inventory")

    monkeypatch.setattr(manager, "_tracked_attachment_paths", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    assert manager.recover_attachment_deletions() == ()


@pytest.mark.parametrize("source_issue", ["missing", "fixed_changed"])
def test_restore_of_good_backup_preserves_incomplete_previous_root(tmp_path, source_issue):
    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        task = env.tasks.create(TaskDraft(title="복구할 원본"))
        attached = add_file(env, task.id, "원본.txt")
        original, _version_id, path = frozen_fixture(env, attached)
        manager = BackupManager(paths)
        backup = manager.create_backup()
        extra = add_file(env, task.id, "백업 이후 작업.txt")
        if source_issue == "missing":
            path.unlink()
        else:
            path.write_bytes(b"changed, but never discard silently")
        env.engine.dispose()
        manager.stage_restore(backup.path)
        assert manager.apply_pending_restore() is None
        assert path.read_bytes() == b"frozen bytes"
        preserved = list(paths.backup_dir.glob("pre-restore-preserved-*"))
        assert len(preserved) == 1
        assert (preserved[0] / "attachments" / extra.relative_path).read_bytes() == b"fixture bytes"
        assert (preserved[0] / "data" / "officeflow.db").is_file()
        assert any("정상 전체 백업 아님" in warning for warning in manager.restore_warnings)
        if source_issue == "fixed_changed":
            assert (
                preserved[0] / "attachments" / original.relative_path
            ).read_bytes() == b"changed, but never discard silently"
        assert not paths.pending_restore_file.exists()


def test_crash_after_preserving_pre_restore_root_does_not_purge_it_on_restart(
    tmp_path, monkeypatch
):
    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        task = env.tasks.create(TaskDraft(title="복구 후 정리 중단"))
        attached = add_file(env, task.id, "기존.txt")
        manager = BackupManager(paths)
        backup = manager.create_backup()
        extra = add_file(
            env, task.id, "지우면 안 되는 추가파일.txt", content=b"distinct additional bytes"
        )
        (paths.attachment_dir / attached.relative_path).unlink()
        env.engine.dispose()
    manager.stage_restore(backup.path)
    original_unlink = type(paths.root).unlink

    def abrupt(path, *args, **kwargs):
        if path == manager._restore_state_file:
            raise SystemExit("after preserved-directory rename")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(paths.root), "unlink", abrupt)
        with pytest.raises(SystemExit):
            manager.apply_pending_restore()
    preserved = list(paths.backup_dir.glob("pre-restore-preserved-*"))
    assert len(preserved) == 1
    manager.apply_pending_restore()
    assert (
        preserved[0] / "attachments" / extra.relative_path
    ).read_bytes() == b"distinct additional bytes"
    assert (paths.attachment_dir / attached.relative_path).read_bytes() == b"fixture bytes"
    assert not manager._restore_state_file.exists()


def test_heavy_import_queues_behind_backup_without_holding_publication_gate(tmp_path, monkeypatch):
    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        task = env.tasks.create(TaskDraft(title="제한된 작업 큐"))
        existing = add_file(env, task.id, "첨부.txt")
        manager = BackupManager(paths)
        original = manager._stage_attachment_sources
        waiting, finished = Event(), Event()
        errors = []
        source = tmp_path / "new.txt"
        source.write_bytes(b"new bytes")

        def add():
            waiting.set()
            try:
                env.attachments.attach(task.id, source)
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        worker = Thread(target=add)

        def stage(captured, target, canceled):
            worker.start()
            assert waiting.wait(1)
            assert not finished.wait(0.1)
            # Metadata and ordinary task operations remain available while queued.
            assert env.tasks.get(task.id).title == "제한된 작업 큐"
            assert env.attachments.get_metadata(existing.id).name == "첨부.txt"
            return original(captured, target, canceled)

        monkeypatch.setattr(manager, "_stage_attachment_sources", stage)
        try:
            manager.create_backup()
        finally:
            if worker.ident is not None:
                worker.join(timeout=5)
        assert finished.is_set() and errors == []
