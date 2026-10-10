from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.data_inspection import DataInspectionService
from officeflow.application.task_snapshots import SnapshotCommand, SnapshotSelection
from officeflow.infrastructure.backup import BackupCanceledError, BackupError, BackupManager
from officeflow.infrastructure.data_inspection import (
    ManagedInspectionReader,
    SqliteInspectionRepository,
)
from officeflow.infrastructure.database.models import SnapshotFileRecord
from tests.integration.test_attachment_documents import command, documents, seed
from tests.integration.test_task_snapshots import service as snapshot_service


def inspector(env):
    return DataInspectionService(
        SqliteInspectionRepository(env.database),
        ManagedInspectionReader(env.attachments.storage_root, env.attachments.coordinator),
    )


def test_actual_files_shared_references_detached_trash_and_read_only(tmp_path):
    with documents(tmp_path) as (env, creation):
        original = seed(env)
        version = creation.execute(command(original, DocumentOperation.VERSION, label="공유"))
        creation.execute(command(original, DocumentOperation.ORIGINAL))
        original = env.attachments.get_metadata(original.id)
        snapshot_service(env).create(
            SnapshotCommand(
                uuid4().hex,
                original.task_id,
                "공유",
                (
                    SnapshotSelection("attachment", original.id),
                    SnapshotSelection("version", version.version_id),
                ),
                documents_closed=True,
            )
        )
        env.tasks.move_to_trash(original.task_id)
        before = env.database.read_bytes()
        hashes = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in env.attachments.storage_root.rglob("*")
            if path.is_file()
        }
        report = inspector(env).run(lambda: False)
        assert report.checked == 3  # shared original, shared version, retained detached mutable
        assert report.counts == {"normal": 3}
        assert before == env.database.read_bytes()
        assert hashes == {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in env.attachments.storage_root.rglob("*")
            if path.is_file()
        }


@pytest.mark.parametrize(
    "mode,status",
    [
        ("missing", "missing"),
        ("changed", "frozen_changed"),
        ("protected", "unavailable"),
        ("mismatch", "reference_mismatch"),
    ],
)
def test_snapshot_only_problem_classification(tmp_path, mode, status):
    with documents(tmp_path) as (env, _creation):
        source = seed(env)
        snapshot = snapshot_service(env).create(
            SnapshotCommand(
                uuid4().hex,
                source.task_id,
                "검사",
                (SnapshotSelection("attachment", source.id),),
                documents_closed=True,
            )
        )
        with env.sessions.transaction() as session:
            row = session.scalar(
                select(SnapshotFileRecord).where(
                    SnapshotFileRecord.snapshot_id == snapshot.snapshot_id
                )
            )
            relative = row.asset.relative_path
            if mode == "mismatch":
                session.connection().exec_driver_sql("DROP TRIGGER snapshot_file_immutable")
                row.fixed_checksum = "1" * 64
        env.attachments.delete_file(source.id)
        path = env.attachments.storage_root / relative
        if mode == "missing":
            path.unlink()
        elif mode == "changed":
            path.write_bytes(b"bad bytes")
        elif mode == "protected":
            env.attachments.coordinator.protect("test", (relative,))
        report = inspector(env).run(lambda: False)
        assert report.checked == 1
        assert report.counts == {status: 1}
        assert report.issues[0].target.kinds == "중요 보관"


def test_mutable_edit_no_baseline_and_open_writer(tmp_path):
    with documents(tmp_path) as (env, _creation):
        source = seed(env)
        path = env.attachments.path_for_open(source.id)
        path.write_bytes(b"legitimate edit")
        assert inspector(env).run(lambda: False).counts == {"modified": 1}
        with path.open("wb"):
            assert inspector(env).run(lambda: False).counts == {"unavailable": 1}
        with env.sessions.transaction() as session:
            from officeflow.infrastructure.database.models import AttachmentRecord

            row = session.get(AttachmentRecord, source.id)
            row.checksum = None
            row.asset.observed_checksum = None
            row.asset.observed_size_bytes = None
        assert inspector(env).run(lambda: False).counts == {"no_baseline": 1}


def test_scope_excludes_later_assets_and_invalid_path(tmp_path):
    with documents(tmp_path) as (env, _creation):
        source = seed(env)
        repository = SqliteInspectionRepository(env.database)
        upper, total = repository.scope()
        newer = seed(env, b"later")
        assert newer.asset_id != source.asset_id
        assert sum(len(repository.page("", upper)) for _ in range(1)) == total == 1
        target = repository.current(source.asset_id)
        reader = ManagedInspectionReader(env.attachments.storage_root, env.attachments.coordinator)
        assert (
            reader.inspect(replace(target, relative_path="../private.txt"), lambda: False)
            == "unavailable"
        )


def test_backup_tracks_failure_success_and_cancellation(tmp_path):
    with documents(tmp_path) as (env, _creation):
        source = seed(env)
        from officeflow.bootstrap.paths import AppPaths

        paths = AppPaths(tmp_path / "backup-profile")
        # search_environment owns database outside AppPaths; use a separate target instance.
        manager = BackupManager(paths)
        # Isolated known profile with the actual test DB and attachment root.
        paths.ensure_directories()
        import shutil
        import sqlite3
        from contextlib import closing

        with (
            closing(sqlite3.connect(env.database)) as source_db,
            closing(sqlite3.connect(paths.database_file)) as target_db,
        ):
            source_db.backup(target_db)
        shutil.copytree(env.attachments.storage_root, paths.attachment_dir, dirs_exist_ok=True)
        missing = paths.attachment_dir / source.relative_path
        content = missing.read_bytes()
        missing.unlink()
        with pytest.raises(BackupError):
            manager.create_backup(reason="automatic")
        assert manager.health.snapshot().attention
        missing.parent.mkdir(parents=True, exist_ok=True)
        missing.write_bytes(content)
        backup = manager.create_backup(reason="automatic")
        assert backup.path.exists() and not manager.health.snapshot().attention
        with pytest.raises(BackupCanceledError):
            manager.create_backup(cancel_requested=lambda: True)
        assert any(row.status == "canceled" for row in manager.health.snapshot().attempts)


def test_native_fault_child_records_and_rotation(tmp_path):
    script = """
import ctypes, faulthandler, os, sys
from pathlib import Path
from officeflow.bootstrap.diagnostics import RuntimeDiagnostics
faulthandler.disable()
d=RuntimeDiagnostics(Path(sys.argv[1])); d.install()
ctypes.windll.kernel32.SetErrorMode(3)
ctypes.CDLL('ucrtbase')._set_abort_behavior(0, 3)
os.abort()
"""
    (tmp_path / "native-fault.log").write_text("previous")
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True, timeout=20
    )
    assert completed.returncode != 0
    assert (tmp_path / "native-fault.log.1").read_text() == "previous"
    assert "Current thread" in (tmp_path / "native-fault.log").read_text()
