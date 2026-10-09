"""Snapshot references participate in existing file/export/delete/backup contracts."""

import json
import subprocess
import sys
from dataclasses import asdict
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.attachment_export import (
    AttachmentExportService,
    ExportStatus,
    FileReference,
)
from officeflow.application.attachments import AttachmentOperationError
from officeflow.application.task_history import HistoryQuery
from officeflow.application.task_snapshots import SnapshotCommand, SnapshotSelection
from officeflow.application.tasks import TaskDraft
from officeflow.bootstrap.paths import AppPaths
from officeflow.domain.task_history import encode
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
from officeflow.infrastructure.attachments.internal_storage import InternalFileStorage
from officeflow.infrastructure.attachments.read_lease import exclusive_read
from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.database.models import (
    FileAssetRecord,
    SnapshotFileRecord,
    TaskSnapshotRecord,
)
from tests.integration.test_attachment_deletion import add_shared_version
from tests.integration.test_attachment_documents import seed
from tests.integration.test_document_lifecycle import lifecycle, original


def capture(facade, task_id, selections=()):
    command = SnapshotCommand(
        uuid4().hex, task_id, "팀장 검토 전", tuple(selections), documents_closed=bool(selections)
    )
    return command, facade.snapshots.create(command)


def test_snapshot_open_export_and_work_survive_deleted_sources(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        source = original(env, facade)
        _command, saved = capture(
            facade, source.task_id, (SnapshotSelection("attachment", source.id),)
        )
        files = facade.catalog.snapshot_files(source.task_id, saved.snapshot_id).items
        file = files[0]
        env.attachments.delete_file(source.id)
        path = env.attachments.storage_root / file.file.relative_path
        view = facade.prepare_open("snapshot_file", file.id)
        assert view != path and view.read_bytes() == b"initial"
        view.write_bytes(b"external edit")
        assert path.read_bytes() == b"initial"
        exports = AttachmentExportService(
            env.attachments, ExternalExportStorage(tmp_path / "journal")
        )
        out = tmp_path / "outside"
        out.mkdir()
        exported = exports.export(
            (FileReference.snapshot_file(file.id),), out / "送付.txt", single=True
        )
        assert exported.items[0].status is ExportStatus.SAVED
        assert exported.items[0].output_path.read_bytes() == b"initial"
        result = facade.create(file, DocumentOperation.WORK, filename="再作成.txt")
        work = env.attachments.get_metadata(result.attachment_id)
        assert work.role.value == "work" and work.family_id == source.family_id
        assert (env.attachments.storage_root / work.relative_path).read_bytes() == b"initial"


def test_last_snapshot_reference_and_task_deletion_consent(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        source = original(env, facade)
        _, first = capture(facade, source.task_id, (SnapshotSelection("attachment", source.id),))
        _, second = capture(facade, source.task_id, (SnapshotSelection("attachment", source.id),))
        env.attachments.delete_file(source.id)
        path = env.attachments.storage_root / source.relative_path
        plan = facade.plan("snapshot", first.snapshot_id)
        assert (
            plan.snapshot_count == 1 and plan.snapshot_file_count == 1 and plan.removable_count == 0
        )
        facade.delete(plan)
        assert path.is_file()
        env.tasks.move_to_trash(source.task_id)
        plan = facade.plan("task", source.task_id)
        assert (
            plan.history_count == 1 and plan.snapshot_count == 1 and plan.snapshot_file_count == 1
        )
        with pytest.raises(AttachmentOperationError, match="범위"):
            facade.delete(plan)
        facade.delete(plan, include_versions=True)
        assert not path.exists()
        with env.sessions.transaction() as session:
            assert session.get(TaskSnapshotRecord, second.snapshot_id) is None


def test_snapshot_only_asset_full_backup_restore_and_uncommitted_recovery(tmp_path):
    paths = AppPaths(tmp_path / "app")
    paths.ensure_directories()
    with lifecycle(paths.data_dir, attachment_root=paths.attachment_dir) as (env, facade):
        source = original(env, facade)
        _, saved = capture(facade, source.task_id, (SnapshotSelection("attachment", source.id),))
        env.attachments.delete_file(source.id)
        mutable = seed(env)
        pending = SnapshotCommand(
            uuid4().hex,
            mutable.task_id,
            "중단",
            (SnapshotSelection("attachment", mutable.id),),
            documents_closed=True,
        )
        facade.snapshots.storage.checkpoint = lambda stage: (
            (_ for _ in ()).throw(OSError("stop")) if stage == "prepared" else None
        )
        with pytest.raises(OSError):
            facade.snapshots.create(pending)
        backup = BackupManager(paths, coordinator=env.attachments.coordinator).create_backup()
        assert backup.table_counts["task_snapshots"] == 1
        assert backup.table_counts["snapshot_files"] == 1
        usage = BackupManager(paths).inspect_data_usage()
        assert usage.snapshot_count == 1 and usage.snapshot_file_count == 1
        manifest = BackupManager(paths).verify_backup(backup.path)
        assert any(
            item.category == "recovery" and "snapshot-" in item.path
            for item in manifest.attachments
        )
        BackupManager(paths).stage_restore(backup.path)
    manager = BackupManager(paths)
    manager.apply_pending_restore()
    assert manager.restore_warnings
    with lifecycle(paths.data_dir, attachment_root=paths.attachment_dir) as (env, facade):
        files = facade.catalog.snapshot_files(source.task_id, saved.snapshot_id).items
        assert (
            len(files) == 1
            and (paths.attachment_dir / files[0].file.relative_path).read_bytes() == b"initial"
        )
        assert not facade.snapshots.pending(mutable.task_id)
        assert list((paths.root / "recovery").rglob("journal.json"))


@pytest.mark.parametrize("failure", ["locked", "checklist", "name", "second_missing"])
def test_multiple_files_refused_atomically_and_source_unchanged(tmp_path, failure):
    with lifecycle(tmp_path) as (env, facade):
        first = seed(env)
        other_path = tmp_path / "second.txt"
        other_path.write_bytes(b"second")
        second = env.attachments.attach(first.task_id, other_path)
        command = SnapshotCommand(
            uuid4().hex,
            first.task_id,
            "二資料",
            (SnapshotSelection("attachment", first.id), SnapshotSelection("attachment", second.id)),
            documents_closed=True,
        )

        def change(stage):
            if stage == "prepared":
                if failure == "checklist":
                    env.records.add_checklist_item(first.task_id, "追加")
                elif failure == "name":
                    env.attachments.rename_attachment(
                        second.id, "새 이름.txt", expected_revision=second.metadata_revision
                    )

        facade.snapshots.storage.checkpoint = change
        if failure == "locked":
            with (
                exclusive_read(env.attachments.storage_root / second.relative_path),
                pytest.raises(OSError),
            ):
                facade.snapshots.create(command)
        else:
            if failure == "second_missing":
                (env.attachments.storage_root / second.relative_path).unlink()
            with pytest.raises((ValueError, OSError)):
                facade.snapshots.create(command)
        with env.sessions.transaction() as session:
            assert session.scalar(select(func.count()).select_from(TaskSnapshotRecord)) == 0
            assert session.scalar(select(func.count()).select_from(SnapshotFileRecord)) == 0
        assert (env.attachments.storage_root / first.relative_path).read_bytes() == b"initial"


@pytest.mark.parametrize(
    "boundary", ["journal", "copied", "prepared", "published", "committed", "finished"]
)
def test_real_process_death_and_owned_parent_recovery(tmp_path, boundary):
    with lifecycle(tmp_path) as (env, facade):
        source = seed(env)
        other_path = tmp_path / "二.txt"
        other_path.write_bytes(b"second")
        second = env.attachments.attach(source.task_id, other_path)
        request = SnapshotCommand(
            uuid4().hex,
            source.task_id,
            "boundary",
            (
                SnapshotSelection("attachment", source.id),
                SnapshotSelection("attachment", second.id),
            ),
            documents_closed=True,
        )
        database, root = env.database, env.attachments.storage_root
    code = """
import json, os, sys
from pathlib import Path
from officeflow.application.task_snapshots import SnapshotCommand, SnapshotSelection
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_snapshot_repository import TaskSnapshotService
from officeflow.infrastructure.attachments.snapshot_storage import SnapshotStorage
from officeflow.application.file_operations import FileOperationCoordinator
raw=json.loads(sys.argv[3])
cmd=SnapshotCommand(raw['job_id'],raw['task_id'],raw['label'],tuple(SnapshotSelection(x['kind'],x['id']) for x in raw['selections']),documents_closed=True)
engine=create_database_engine(Path(sys.argv[1]))
store=SnapshotStorage(Path(sys.argv[2]),FileOperationCoordinator(),lambda stage:os._exit(74) if stage==sys.argv[4] else None)
TaskSnapshotService(SessionFactory(engine),store).create(cmd)
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(database), str(root), encode(asdict(request)), boundary],
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 74, process.stderr.decode(errors="replace")
    with lifecycle(tmp_path) as (env, facade):
        committed = facade.snapshots.committed(request)
        assert (root / source.relative_path).read_bytes() == b"initial"
        assert (root / second.relative_path).read_bytes() == b"second"
        if boundary in {"committed", "finished"}:
            assert committed is not None
            assert facade.snapshots.create(request).already_committed
            if boundary == "committed":
                InternalFileStorage(root, coordinator=env.attachments.coordinator).preflight()
                report = facade.snapshots.recover(request.job_id)
                assert report.recovered == 1
            assert not facade.snapshots.storage.folder(request.job_id).exists()
            assert (
                len(facade.catalog.snapshot_files(source.task_id, committed.snapshot_id).items) == 2
            )
        else:
            assert committed is None
            report = facade.snapshots.recover(request.job_id)
            assert report.pending == 1
            facade.snapshots.discard_pending(source.task_id, request.job_id, approved=True)
            assert not facade.snapshots.storage.folder(request.job_id).exists()
            assert not env.attachments.coordinator.is_protected(source.relative_path)
            with env.sessions.transaction() as session:
                assert session.scalar(select(func.count()).select_from(TaskSnapshotRecord)) == 0


def test_modified_pending_and_unknown_journal_are_preserved(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        source = seed(env)
        command = SnapshotCommand(
            uuid4().hex,
            source.task_id,
            "중단",
            (SnapshotSelection("attachment", source.id),),
            documents_closed=True,
        )
        facade.snapshots.storage.checkpoint = lambda stage: (
            (_ for _ in ()).throw(OSError("stop")) if stage == "prepared" else None
        )
        with pytest.raises(OSError):
            facade.snapshots.create(command)
        raw = facade.snapshots.storage.read(command.job_id)
        part = env.attachments.storage_root / raw["assets"][0]["part"]
        part.write_bytes(b"do not delete")
        with pytest.raises(ValueError, match="변경"):
            facade.snapshots.discard_pending(source.task_id, command.job_id, approved=True)
        assert part.read_bytes() == b"do not delete"
        raw["format_version"] = 999
        journal = facade.snapshots.storage.folder(command.job_id) / "journal.json"
        journal.write_text(json.dumps(raw), encoding="utf-8")
        preflight = InternalFileStorage(
            env.attachments.storage_root, coordinator=env.attachments.coordinator
        ).preflight()
        assert "보존" in preflight[0].message
        assert journal.is_file()


def test_shared_frozen_asset_has_two_logical_files_but_one_physical_copy(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        source = original(env, facade)
        version = add_shared_version(env, source)
        with env.sessions.transaction() as session:
            before_count = session.scalar(select(func.count()).select_from(FileAssetRecord))
        _, saved = capture(
            facade,
            source.task_id,
            (SnapshotSelection("attachment", source.id), SnapshotSelection("version", version)),
        )
        files = facade.catalog.snapshot_files(source.task_id, saved.snapshot_id).items
        assert len(files) == 2 and len({file.file.asset_id for file in files}) == 1
        with env.sessions.transaction() as session:
            assert session.scalar(select(func.count()).select_from(FileAssetRecord)) == before_count


def test_mixed_equal_timestamp_keyset_and_filters_apply_before_branch_limit(tmp_path):
    from datetime import UTC, datetime

    from sqlalchemy import insert

    from officeflow.infrastructure.database.models import TaskHistoryRecord

    with lifecycle(tmp_path) as (env, _facade):
        now = datetime.now(UTC)
        task = env.tasks.create(TaskDraft("tied"), now=now)
        values = env.tasks.history.current(task.id)
        with env.sessions.transaction() as session:
            session.execute(
                insert(TaskSnapshotRecord),
                [
                    dict(
                        task_id=task.id,
                        scope="task",
                        scope_key="task",
                        occurrence_start=None,
                        label="tied",
                        ordinal=index + 1,
                        captured_at=now,
                        committed_at=now,
                        job_id=uuid4().hex,
                        request_fingerprint="a" * 64,
                        payload_version=1,
                        info_payload=encode({"values": values, "checklist": []}),
                        file_count=0,
                        summary="tied",
                    )
                    for index in range(7)
                ],
            )
            session.execute(
                insert(TaskHistoryRecord),
                [
                    dict(
                        task_id=task.id,
                        scope="task",
                        scope_key="task",
                        occurrence_start=None,
                        command_id=uuid4().hex,
                        event_type="change",
                        recorded_at=now,
                        changed_fields='["description"]' if index == 0 else '["title"]',
                        before_payload=encode(values),
                        after_payload=encode(values),
                        payload_version=1,
                        summary="변경",
                    )
                    for index in range(7)
                ],
            )
        query = HistoryQuery(limit=5)
        identities = []
        while True:
            page = env.tasks.history.page(task.id, query)
            identities.extend((item.kind, item.id) for item in page.items)
            if not page.has_next:
                break
            last = page.items[-1]
            query = HistoryQuery(
                limit=5, cursor=(last.recorded_at, 1 if last.kind == "snapshot" else 0, last.id)
            )
        assert len(identities) == len(set(identities)) == 15
        assert all(kind == "snapshot" for kind, _ in identities[:7])
        filtered = env.tasks.history.page(
            task.id, HistoryQuery(changed_field="description", limit=5)
        )
        assert (
            len(filtered.items) == 2
        )  # create includes all fields, plus the oldest matching change
        assert all(item.kind == "history" for item in filtered.items)
        only_saved = env.tasks.history.page(task.id, HistoryQuery(event_type="snapshot", limit=5))
        assert len(only_saved.items) == 5 and only_saved.has_next


def test_selection_limit_duplicates_and_closed_confirmation():
    with pytest.raises(ValueError):
        SnapshotCommand(
            uuid4().hex,
            1,
            "large",
            tuple(SnapshotSelection("attachment", index) for index in range(1, 102)),
            documents_closed=True,
        )
    with pytest.raises(ValueError):
        SnapshotCommand(
            uuid4().hex,
            1,
            "duplicate",
            (SnapshotSelection("attachment", 1),) * 2,
            documents_closed=True,
        )
    with pytest.raises(ValueError):
        SnapshotCommand(uuid4().hex, 1, "open document", (SnapshotSelection("attachment", 1),))
