"""Phase 2 uses isolated profiles, real backups and independent external outputs."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import zipfile
from contextlib import closing
from uuid import uuid4

import pytest
from sqlalchemy import text, update

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.attachment_export import ExportStatus
from officeflow.application.attachment_search import AttachmentSearchQuery
from officeflow.application.task_snapshots import SnapshotCommand, SnapshotSelection
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.backup import BackupCanceledError, BackupError, BackupManager
from officeflow.infrastructure.backup_files import BackupFileSession
from officeflow.infrastructure.database.models import (
    AttachmentRecord,
    AttachmentVersionRecord,
    TaskSnapshotRecord,
)
from tests.integration.legacy_attachment_fixture import legacy_environment
from tests.integration.test_attachment_documents import command, documents, seed
from tests.integration.test_task_snapshots import service as snapshot_service


def FALSE():
    return False


def populate(env, creation):
    attachment = seed(env, b"current bytes")
    attachment = env.attachments.update_memo(
        attachment.id, "공통메모 100%_실제", expected_revision=attachment.metadata_revision
    )
    version = creation.execute(
        command(attachment, DocumentOperation.VERSION, label="검토 반영", memo="보관전용 메모")
    )
    snapshot = snapshot_service(env).create(
        SnapshotCommand(
            uuid4().hex,
            attachment.task_id,
            "제출 전 확정",
            (SnapshotSelection("attachment", attachment.id),),
            documents_closed=True,
        )
    )
    return attachment, version, snapshot


def test_search_all_roles_metadata_and_literal_wildcards(tmp_path):
    with documents(tmp_path) as (env, creation):
        attachment, _version, _snapshot = populate(env, creation)
        all_hits = env.search.search_page(AttachmentSearchQuery(search="서식")).items
        assert {hit.source_kind for hit in all_hits} == {"attachment", "version", "snapshot_file"}
        assert len({hit.reference for hit in all_hits}) == 3
        assert len(env.search.search_page(AttachmentSearchQuery(search="공통메모")).items) == 2
        assert len(env.search.search_page(AttachmentSearchQuery(search="100%_실제")).items) == 2
        assert not env.search.search_page(AttachmentSearchQuery(search="100__")).items
        assert (
            env.search.search_page(AttachmentSearchQuery(search="검토 반영")).items[0].source_kind
            == "version"
        )
        assert (
            env.search.search_page(AttachmentSearchQuery(search="제출 전")).items[0].source_kind
            == "snapshot_file"
        )
        assert (
            len(env.search.search_page(AttachmentSearchQuery(source_kind="attachment")).items) == 1
        )
        env.attachments.rename_attachment(
            attachment.id, "새이름.txt", expected_revision=attachment.metadata_revision
        )
        assert len(env.search.search_page(AttachmentSearchQuery(search="새이름")).items) == 1
        assert (
            len(env.search.search_page(AttachmentSearchQuery(search="서식")).items) == 3
        )  # original import name included
        env.tasks.move_to_trash(attachment.task_id)
        assert not env.search.search_page(AttachmentSearchQuery()).items
        assert len(env.search.search_page(AttachmentSearchQuery(include_trash=True)).items) == 3


def test_search_tie_cursor_keeps_same_ids_and_timestamp(tmp_path):
    with documents(tmp_path) as (env, creation):
        attachment, _version, _snapshot = populate(env, creation)
        with env.sessions.transaction() as db:
            # Isolated synthetic tie fixture only; production protection is unchanged.
            db.execute(text("DROP TRIGGER version_capture_immutable"))
            db.execute(text("DROP TRIGGER snapshot_immutable"))
            db.execute(update(AttachmentVersionRecord).values(created_at=attachment.created_at))
            db.execute(update(TaskSnapshotRecord).values(captured_at=attachment.created_at))
        hits = []
        cursor = None
        while True:
            page = env.search.search_page(AttachmentSearchQuery(limit=1, cursor=cursor))
            hits.extend(page.items)
            cursor = page.next_cursor
            if not page.has_more:
                break
        assert len(hits) == 3 and len({hit.reference for hit in hits}) == 3
        assert {hit.source_kind for hit in hits} == {"attachment", "version", "snapshot_file"}


def test_backup_select_all_types_snapshot_only_recovery_and_no_live_writes(tmp_path):
    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    with documents(paths.data_dir, attachment_root=paths.attachment_dir) as (env, creation):
        attachment, _version, snapshot = populate(env, creation)
        # Test-only removal through supported snapshot-preserving metadata references.
        with env.sessions.transaction() as db:
            row = db.get(AttachmentRecord, attachment.id)
            db.delete(row)
        orphan = paths.attachment_dir / "unclassified.txt"
        orphan.write_bytes(b"recovery")
        manager = BackupManager(paths)
        backup = manager.create_backup()
        before = hashlib.sha256(env.database.read_bytes()).digest()
        session = BackupFileSession(manager, backup.path)
        try:
            page = session.load(FALSE)
            assert {hit.kind for hit in page.items} == {"version", "snapshot_file", "recovery"}
            assert session.page("제출 전", canceled=FALSE).items[0].kind == "snapshot_file"
            output = tmp_path / "external"
            output.mkdir()
            (output / "서식.txt").write_bytes(b"existing")
            result = session.export(tuple(hit.id for hit in page.items), output, FALSE)
            assert all(item.status is ExportStatus.SAVED for item in result.items)
            assert (output / "서식.txt").read_bytes() == b"existing"
            assert {item.output_path.read_bytes() for item in result.items} == {
                b"current bytes",
                b"recovery",
            }
            assert hashlib.sha256(env.database.read_bytes()).digest() == before
            assert not paths.pending_restore_file.exists()
            assert len(manager.verify_backup(backup.path).attachments) >= 3
            assert snapshot.snapshot_id > 0
        finally:
            temporary = session.root
            session.close()
            assert not temporary.exists()


@pytest.mark.parametrize("schema", ["0009_reminder_schedule_cache", "0012_attachment_observation"])
def test_old_backup_schema_read_without_migration(tmp_path, schema):
    paths = AppPaths(tmp_path / "old")
    paths.ensure_directories()
    with legacy_environment(paths.data_dir, schema, attachment_root=paths.attachment_dir) as env:
        from officeflow.application.tasks import TaskDraft

        task = env.tasks.create(TaskDraft(title="지난 업무"))
        env.add(task.id, "과거자료.txt", content=b"old")
    manager = BackupManager(paths)
    backup = manager.create_backup()
    session = BackupFileSession(manager, backup.path)
    try:
        assert session.load(FALSE).items[0].name == "과거자료.txt"
        with closing(sqlite3.connect(session.root / "source.db")) as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == schema
        output = tmp_path / "old-export"
        output.mkdir()
        result = session.export((1,), output, FALSE)
        assert result.items[0].output_path.read_bytes() == b"old"
    finally:
        session.close()


def one_backup(tmp_path, payload=b"data"):
    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    with documents(paths.data_dir, attachment_root=paths.attachment_dir) as (env, _):
        seed(env, payload)
        manager = BackupManager(paths)
        backup = manager.create_backup()
    return manager, backup.path


def test_corrupt_duplicate_and_unsafe_backups_rejected(tmp_path):
    manager, backup = one_backup(tmp_path)
    for mode in ("duplicate", "unsafe", "bad-digest"):
        bad = tmp_path / f"{mode}.ofbackup"
        with zipfile.ZipFile(backup) as original, zipfile.ZipFile(bad, "w") as changed:
            for name in original.namelist():
                data = original.read(name)
                if mode == "bad-digest" and name.startswith("attachments/"):
                    data = b"x" * len(data)
                changed.writestr(name, data)
            if mode == "unsafe":
                changed.writestr("attachments/../escape.txt", b"x")
            if mode == "duplicate":
                with pytest.warns(UserWarning):
                    changed.writestr("manifest.json", original.read("manifest.json"))
        session = BackupFileSession(manager, bad)
        with pytest.raises(BackupError):
            session.load(FALSE)
        assert session._temporary is None
    assert not manager._paths.pending_restore_file.exists()


def test_cancel_cleanup_failure_reports_review_and_preserves_owned_prepared_copy(
    tmp_path, monkeypatch
):
    from officeflow.application.attachments import AttachmentCanceledError
    from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage

    manager, archive = one_backup(tmp_path)
    session = BackupFileSession(manager, archive)
    try:
        page = session.load(FALSE)
        folder = tmp_path / "cancel-output"
        folder.mkdir()

        def cancel_publish(*args, **kwargs):
            raise AttachmentCanceledError("취소")

        def fail_cleanup(*args, **kwargs):
            raise PermissionError("locked")

        monkeypatch.setattr(ExternalExportStorage, "publish", cancel_publish)
        monkeypatch.setattr(ExternalExportStorage, "abort", fail_cleanup)
        result = session.export((page.items[0].id,), folder, FALSE)
        assert result.items[0].status is ExportStatus.NEEDS_REVIEW
        assert result.items[0].journal_path.exists()
        assert not (folder / "서식.txt").exists()
        assert any(path.read_bytes() == b"data" for path in folder.iterdir())
    finally:
        session.close()


def test_selected_archive_payload_reads_are_bounded(tmp_path, monkeypatch):
    manager, archive = one_backup(tmp_path, b"z" * (4 * 1024**2))
    session = BackupFileSession(manager, archive)
    try:
        page = session.load(FALSE)
        reads = []
        original_read = zipfile.ZipExtFile.read

        def measured(self, n=-1):
            if self.name.startswith("attachments/"):
                reads.append(n)
            return original_read(self, n)

        monkeypatch.setattr(zipfile.ZipExtFile, "read", measured)
        folder = tmp_path / "out"
        folder.mkdir()
        result = session.export((page.items[0].id,), folder, FALSE)
        assert result.items[0].status is ExportStatus.SAVED
        assert len(reads) >= 4 and set(reads) == {1024**2}
    finally:
        session.close()


@pytest.mark.parametrize("limited", ["manifest.json", "data/officeflow.db"])
def test_preflight_rejects_oversized_catalog_before_payload_read(tmp_path, monkeypatch, limited):
    manager, archive = one_backup(tmp_path)
    original = zipfile.ZipFile.getinfo

    def oversized(self, name):
        info = original(self, name)
        if name == limited:
            info.file_size = 513 * 1024**2
        return info

    monkeypatch.setattr(zipfile.ZipFile, "getinfo", oversized)
    session = BackupFileSession(manager, archive)
    with pytest.raises(BackupError, match=r"너무|한도"):
        session.load(FALSE)
    assert session._temporary is None


def test_format1_selection_keeps_supported_old_database(tmp_path):
    import json

    from officeflow.application.tasks import TaskDraft

    paths = AppPaths(tmp_path / "old-format")
    paths.ensure_directories()
    with legacy_environment(
        paths.data_dir, "0009_reminder_schedule_cache", attachment_root=paths.attachment_dir
    ) as env:
        task = env.tasks.create(TaskDraft(title="구형"))
        env.add(task.id, "자료.txt", content=b"legacy")
    manager = BackupManager(paths)
    backup = manager.create_backup()
    old_format = tmp_path / "format1.ofbackup"
    with zipfile.ZipFile(backup.path) as source, zipfile.ZipFile(old_format, "w") as target:
        for name in source.namelist():
            data = source.read(name)
            if name == "manifest.json":
                manifest = json.loads(data)
                manifest["format_version"] = 1
                manifest.pop("missing_attachments", None)
                data = json.dumps(manifest).encode()
            target.writestr(name, data)
    session = BackupFileSession(manager, old_format)
    try:
        page = session.load(FALSE)
        assert page.items[0].name == "자료.txt"
        folder = tmp_path / "export1"
        folder.mkdir()
        assert (
            session.export((page.items[0].id,), folder, FALSE).items[0].output_path.read_bytes()
            == b"legacy"
        )
    finally:
        session.close()


def test_archive_changed_and_managed_destination_rejected(tmp_path):
    manager, backup = one_backup(tmp_path)
    session = BackupFileSession(manager, backup)
    try:
        hit = session.load(FALSE).items[0]
        result = session.export((hit.id,), manager.attachment_directory, FALSE)
        assert result.items[0].status is ExportStatus.FAILED
        os.utime(backup, (backup.stat().st_atime, backup.stat().st_mtime + 2))
        with pytest.raises(BackupError, match="변경"):
            session.export((hit.id,), tmp_path, FALSE)
        assert not manager._paths.pending_restore_file.exists()
    finally:
        session.close()


def test_cancel_then_retry_and_completed_files_stay(tmp_path):
    manager, backup = one_backup(tmp_path, b"a" * (3 * 1024**2))
    session = BackupFileSession(manager, backup)
    try:
        hit = session.load(FALSE).items[0]
        folder = tmp_path / "output"
        folder.mkdir()
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            return calls >= 4

        result = session.export((hit.id,), folder, cancel)
        assert result.items[0].status is ExportStatus.UNPROCESSED
        assert not list(folder.iterdir())
        result = session.export((hit.id,), folder, FALSE)
        assert result.items[0].status is ExportStatus.SAVED
        result = session.export((hit.id,), folder, lambda: True)
        assert result.items[0].status is ExportStatus.UNPROCESSED
        assert len(list(folder.iterdir())) == 1
    finally:
        session.close()


def test_load_canceled_has_no_temp_or_reservation(tmp_path):
    manager, backup = one_backup(tmp_path)
    session = BackupFileSession(manager, backup)
    with pytest.raises(BackupCanceledError):
        session.load(lambda: True)
    assert session._temporary is None
    assert not list(manager.backup_directory.parent.glob(".backup-files-*"))


@pytest.mark.parametrize("failure", [PermissionError("denied"), OSError(28, "space")])
def test_output_failure_preserves_archive_and_other_files(tmp_path, monkeypatch, failure):
    from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage

    manager, backup = one_backup(tmp_path)
    session = BackupFileSession(manager, backup)
    folder = tmp_path / "out"
    folder.mkdir()
    existing = folder / "서식.txt"
    existing.write_bytes(b"existing")
    archive_hash = hashlib.sha256(backup.read_bytes()).digest()
    try:
        page = session.load(FALSE)

        def fail(*args, **kwargs):
            raise failure

        monkeypatch.setattr(ExternalExportStorage, "prepare", fail)
        result = session.export((page.items[0].id,), folder, FALSE)
        assert result.items[0].status is ExportStatus.FAILED
        assert existing.read_bytes() == b"existing"
        assert hashlib.sha256(backup.read_bytes()).digest() == archive_hash
    finally:
        session.close()


def test_backup_catalog_pages_are_bounded_and_selection_ids_validated(tmp_path):
    manager, backup = one_backup(tmp_path)
    session = BackupFileSession(manager, backup)
    try:
        session.load(FALSE)
        with closing(sqlite3.connect(session.root / "catalog.db")) as db:
            for number in range(104):
                db.execute(
                    "INSERT INTO files(kind,name,label,memo,task_title,member,size,sha256) SELECT kind,?,label,memo,task_title,member,size,sha256 FROM files WHERE id=1",
                    (f"긴자료 {number}.txt",),
                )
            db.commit()
        first = session.page(canceled=FALSE)
        second = session.page(after=first.items[-1].id, canceled=FALSE)
        assert len(first.items) == 100 and first.has_more
        assert len(second.items) == 5 and not second.has_more
        assert len(session.page("자료 103", canceled=FALSE).items) == 1
        with pytest.raises(ValueError):
            session.export((1, 1), tmp_path, FALSE)
        with pytest.raises(BackupError):
            session.export((999,), tmp_path, FALSE)
    finally:
        session.close()


def test_force_exit_external_copy_keeps_backup_live_data_and_owned_journal(tmp_path):
    import subprocess
    import sys

    manager, backup = one_backup(tmp_path)
    folder = tmp_path / "export"
    folder.mkdir()
    before = hashlib.sha256(manager._paths.database_file.read_bytes()).digest()
    script = """
import os, sys
from pathlib import Path
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.backup_files import BackupFileSession
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
original = ExternalExportStorage._write_journal
def checkpoint(self, prepared, state, **kwargs):
    original(self, prepared, state, **kwargs)
    if state == 'prepared':
        os._exit(71)
ExternalExportStorage._write_journal = checkpoint
manager = BackupManager(AppPaths(Path(sys.argv[1])))
session = BackupFileSession(manager, Path(sys.argv[2]))
page = session.load(lambda: False)
session.export((page.items[0].id,), Path(sys.argv[3]), lambda: False)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(manager._paths.root), str(backup), str(folder)],
        capture_output=True,
        timeout=30,
    )
    assert child.returncode == 71, child.stderr
    assert hashlib.sha256(manager._paths.database_file.read_bytes()).digest() == before
    assert manager.verify_backup(backup)
    from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage

    storage = ExternalExportStorage(manager._paths.root)
    recovery = storage.inspect_recovery(FALSE)
    assert len(recovery) == 1
    assert recovery[0].temporary.exists()
    assert recovery[0].temporary.read_bytes() == b"data"
    assert not (folder / "서식.txt").exists()
    assert not manager._paths.pending_restore_file.exists()
