"""No operational data: exact deletion plans, Windows handles, and process death."""

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from threading import Event, Thread
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text

from officeflow.application.attachment_deletion import (
    AttachmentDeletionService,
    DeleteCommand,
    DeleteSelection,
    DeleteTarget,
)
from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.attachments import AttachmentOperationError
from officeflow.application.file_operations import FileInUseError, FileJobCanceledError
from officeflow.application.tasks import TaskDraft
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.attachments.deletion_storage import DeletionFileStorage
from officeflow.infrastructure.attachments.read_lease import protected_read
from officeflow.infrastructure.backup import BackupError, BackupManager
from officeflow.infrastructure.database.attachment_deletion_repository import (
    SqlAlchemyDeletionRepository,
)
from officeflow.infrastructure.database.models import (
    AttachmentRecord,
    AttachmentVersionRecord,
    FileAssetRecord,
    FileOperationReceiptRecord,
    TaskRecord,
    WorkLogRecord,
)
from tests.integration.test_attachment_documents import command, documents, result_path, seed
from tests.integration.test_attachment_search import search_environment


@contextmanager
def deleting(root, *, checkpoint=None):
    with documents(root) as (env, creation):
        storage = DeletionFileStorage(
            env.attachments.storage_root,
            coordinator=env.attachments.coordinator,
            checkpoint=checkpoint,
        )
        repository = SqlAlchemyDeletionRepository(env.sessions)
        service = AttachmentDeletionService(repository, storage)
        yield env, creation, service, repository, storage


def request(service, kind, id, *, include_versions=None):
    plan = service.plan(DeleteTarget(kind, id))
    if include_versions is None:
        include_versions = kind == "task"
    return DeleteCommand(uuid4().hex, plan.target, plan.fingerprint, include_versions)


def journal_folder(env, cmd):
    return env.attachments.storage_root / ".trash" / ("delete-" + cmd.job_id)


def test_simple_delete_receipt_and_replay_never_delete_a_new_attachment(tmp_path):
    with deleting(tmp_path) as (env, _, service, repository, storage):
        original = seed(env)
        path = env.attachments.path_for_open(original.id)
        cmd = request(service, "attachment", original.id)
        result = service.execute(cmd)
        assert not result.pending and not result.already_committed
        assert not path.exists() and not storage.has_pending()
        with env.sessions.transaction() as session:
            assert session.get(FileAssetRecord, original.asset_id) is None
            assert session.get(AttachmentRecord, original.id) is None
            assert session.get(FileOperationReceiptRecord, cmd.job_id) is not None
        other = seed(env, b"new")
        replay = service.execute(cmd)
        assert replay.already_committed and not replay.pending
        assert env.attachments.path_for_open(other.id).read_bytes() == b"new"
        with pytest.raises(ValueError, match="다른 삭제"):
            repository.committed(replace(cmd, plan_fingerprint="f" * 64))


def test_original_and_work_deletion_keep_independent_versions_and_retained_edit_file(tmp_path):
    with deleting(tmp_path) as (env, creation, service, _, _):
        source = seed(env)
        version = creation.execute(command(source, DocumentOperation.VERSION, label="제출 전"))
        original = creation.execute(command(source, DocumentOperation.ORIGINAL))
        fixed = result_path(env, original)
        archived = result_path(env, version)
        retained = env.attachments.get_metadata(original.retained_attachment_id)
        plan = service.plan(DeleteTarget("attachment", source.id))
        assert plan.attachment_count == 1 and plan.kept_versions == 1
        assert plan.removable_count == 1
        assert not service.execute(
            DeleteCommand(uuid4().hex, plan.target, plan.fingerprint)
        ).pending
        assert not fixed.exists() and archived.read_bytes() == b"initial"
        assert env.attachments.path_for_open(retained.id).read_bytes() == b"initial"
        with env.sessions.transaction() as session:
            saved = session.get(AttachmentVersionRecord, version.version_id)
            assert saved.source_attachment_id is None
            assert json.loads(saved.source_payload)["id"] == source.id


def add_shared_version(env, source):
    with env.sessions.transaction() as session:
        saved = AttachmentVersionRecord(
            task_id=source.task_id,
            family_id=source.family_id,
            asset_id=source.asset_id,
            label="같은 고정 파일",
            suggestion_number=10,
            original_name=source.name,
            memo="보존",
            created_at=datetime.now(UTC),
        )
        session.add(saved)
        session.flush()
        return saved.id


def test_other_content_reference_keeps_file_active_until_last_reference_deleted(tmp_path):
    with deleting(tmp_path) as (env, creation, service, _, _):
        source = seed(env)
        converted = creation.execute(command(source, DocumentOperation.ORIGINAL))
        source = env.attachments.get_metadata(source.id)
        version_id = add_shared_version(env, source)
        path = result_path(env, converted)
        plan = service.plan(DeleteTarget("attachment", source.id))
        assert plan.removable_count == 0 and plan.kept_asset_count == 1
        assert not service.execute(
            DeleteCommand(uuid4().hex, plan.target, plan.fingerprint)
        ).pending
        assert path.read_bytes() == b"initial"
        with env.sessions.transaction() as session:
            assert session.get(FileAssetRecord, source.asset_id).state == "active"
        assert not service.execute(request(service, "version", version_id)).pending
        assert not path.exists()


def test_task_trash_scope_requires_extra_version_confirmation_and_keeps_work_logs(tmp_path):
    with deleting(tmp_path) as (env, creation, service, _, _):
        source = seed(env)
        creation.execute(command(source, DocumentOperation.VERSION, label="중요"))
        now = datetime.now(UTC)
        with env.sessions.transaction() as session:
            log = WorkLogRecord(
                task_id=source.task_id,
                log_date=date.today(),
                content="업무일지는 유지",
                created_at=now,
                updated_at=now,
            )
            session.add(log)
            session.flush()
            log_id = log.id
        with pytest.raises(LookupError, match="휴지통"):
            service.plan(DeleteTarget("task", source.task_id))
        env.tasks.move_to_trash(source.task_id)
        plan = service.plan(DeleteTarget("task", source.task_id))
        assert (plan.attachment_count, plan.version_count, plan.work_log_count) == (1, 1, 1)
        cmd = DeleteCommand(uuid4().hex, plan.target, plan.fingerprint)
        with pytest.raises(AttachmentOperationError, match="별도로"):
            service.execute(cmd)
        assert not journal_folder(env, cmd).exists()
        assert not service.execute(replace(cmd, include_versions=True)).pending
        with env.sessions.transaction() as session:
            assert session.get(TaskRecord, source.task_id) is None
            assert session.get(WorkLogRecord, log_id).task_id is None
            assert session.get(WorkLogRecord, log_id).content == "업무일지는 유지"
            assert session.scalar(select(FileAssetRecord.id)) is None


def test_old_confirmation_rejected_without_touching_bytes(tmp_path):
    with deleting(tmp_path) as (env, _, service, _, _):
        source = seed(env)
        cmd = request(service, "attachment", source.id)
        env.attachments.rename_attachment(
            source.id, "바뀐이름.txt", expected_revision=source.metadata_revision
        )
        with pytest.raises(AttachmentOperationError, match="다시 확인"):
            service.execute(cmd)
        assert not journal_folder(env, cmd).exists()
        assert env.attachments.path_for_open(source.id).read_bytes() == b"initial"


@pytest.mark.parametrize("failure", ["database", "new_reference", "cancel"])
def test_failure_after_quarantine_restores_without_logical_deletion(tmp_path, failure):
    with deleting(tmp_path) as (env, creation, service, repository, storage):
        source = seed(env)
        converted = (
            creation.execute(command(source, DocumentOperation.ORIGINAL))
            if failure == "new_reference"
            else None
        )
        source = env.attachments.get_metadata(source.id)
        path = env.attachments.storage_root / source.relative_path
        cmd = request(service, "attachment", source.id)
        cancel = Event()

        def checkpoint(stage):
            if stage == "moved":
                if failure == "new_reference":
                    add_shared_version(env, source)
                elif failure == "cancel":
                    cancel.set()

        storage._checkpoint = checkpoint

        def fail(_connection, _cursor, statement, *_args):
            if statement.startswith("INSERT INTO file_operation_receipts"):
                raise RuntimeError("receipt write failure")

        if failure == "database":
            event.listen(env.engine, "before_cursor_execute", fail)
        try:
            with pytest.raises((RuntimeError, AttachmentOperationError, FileJobCanceledError)):
                service.execute(cmd, cancel_requested=cancel.is_set)
        finally:
            if failure == "database":
                event.remove(env.engine, "before_cursor_execute", fail)
        assert path.read_bytes() == b"initial"
        assert env.attachments.get_metadata(source.id).asset_id == source.asset_id
        assert repository.committed(cmd) is None and not storage.has_pending()
        if converted:
            assert result_path(env, converted) == path


def test_partial_multiple_quarantine_locked_second_file_rolls_back_all(tmp_path):
    with deleting(tmp_path) as (env, _, service, repository, storage):
        source = seed(env)
        extra_file = tmp_path / "another.txt"
        extra_file.write_bytes(b"second")
        other = env.attachments.attach(source.task_id, extra_file)
        env.tasks.move_to_trash(source.task_id)
        cmd = request(service, "task", source.task_id)
        plan = repository.plan(cmd.target)
        blocked = plan.assets[1]
        with protected_read(env.attachments.storage_root / blocked.path), pytest.raises(OSError):
            service.execute(cmd)
        assert repository.committed(cmd) is None
        assert not storage.has_pending()
        assert env.attachments.path_for_open(source.id).read_bytes() == b"initial"
        assert env.attachments.path_for_open(other.id).read_bytes() == b"second"


def test_read_pin_rejects_delete_and_delete_intent_rejects_reader(tmp_path):
    with deleting(tmp_path) as (env, _, service, _, storage):
        source = seed(env)
        cmd = request(service, "attachment", source.id)
        with env.attachments.coordinator.pin(source.relative_path), pytest.raises(FileInUseError):
            service.execute(cmd)

        def checkpoint(stage):
            if stage == "prepared":
                with (
                    pytest.raises(FileInUseError),
                    env.attachments.coordinator.pin(source.relative_path),
                ):
                    pass

        storage._checkpoint = checkpoint
        assert not service.execute(cmd).pending


def test_unknown_asset_fk_rejects_even_empty_reference_table(tmp_path):
    with deleting(tmp_path) as (env, _, service, _, _):
        source = seed(env)
        with env.sessions.transaction() as session:
            session.execute(
                text(
                    "CREATE TABLE unknown_files(id INTEGER PRIMARY KEY, asset_id TEXT REFERENCES file_assets(id))"
                )
            )
        with pytest.raises(ValueError, match="지원하지 않는"):
            service.plan(DeleteTarget("attachment", source.id))
        assert env.attachments.path_for_open(source.id).read_bytes() == b"initial"


@pytest.mark.parametrize(
    "stage",
    [
        "journal",
        "prepared",
        "moved",
        "quarantined",
        "before_commit",
        "committed",
        "purged",
        "asset_removed",
        "state_removed",
    ],
)
@pytest.mark.parametrize("scope", ["single", "selection"])
def test_actual_process_death_then_restart_restores_or_finishes(tmp_path, stage, scope):
    root = tmp_path / "profile"
    with deleting(root) as (env, _, service, _, _):
        source = seed(env)
        cmd = request(service, "attachment", source.id)
        path = env.attachments.storage_root / source.relative_path
        source_id, asset_id = source.id, source.asset_id
        extra = None
        if scope == "selection":
            file = env.database.parent / "second.txt"
            file.write_bytes(b"second fixture")
            extra = env.attachments.attach(source.task_id, file)
            plan = service.plan(
                DeleteSelection(
                    source.task_id,
                    (DeleteTarget("attachment", source.id), DeleteTarget("attachment", extra.id)),
                )
            )
            cmd = DeleteCommand(uuid4().hex, plan.target, plan.fingerprint)
    child = r"""
import json, os, sys
from pathlib import Path
from sqlalchemy import event
from officeflow.application.attachment_deletion import *
from officeflow.infrastructure.attachments.deletion_storage import DeletionFileStorage
from officeflow.infrastructure.database.attachment_deletion_repository import SqlAlchemyDeletionRepository
from tests.integration.test_attachment_search import search_environment
root, stage, raw = Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
cmd = DeleteCommand(raw['job_id'], decode_delete_target(raw['target']), raw['plan_fingerprint'], raw['include_versions'])
with search_environment(root) as env:
    wrote_receipt = [False]
    def sql(_conn, _cursor, statement, *_args):
        if statement.startswith('INSERT INTO file_operation_receipts'):
            wrote_receipt[0] = True
    def before_commit(_conn):
        if stage == 'before_commit' and wrote_receipt[0]:
            os._exit(75)
    event.listen(env.engine, 'before_cursor_execute', sql)
    event.listen(env.engine, 'commit', before_commit)
    def checkpoint(point):
        if point == stage:
            os._exit(75)
    storage = DeletionFileStorage(env.attachments.storage_root, coordinator=env.attachments.coordinator, checkpoint=checkpoint)
    AttachmentDeletionService(SqlAlchemyDeletionRepository(env.sessions), storage).execute(cmd)
os._exit(1)
"""
    process = subprocess.run(
        [sys.executable, "-c", child, str(root), stage, json.dumps(asdict(cmd))],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 75, process.stdout + process.stderr
    committed = stage in {"committed", "purged", "asset_removed", "state_removed"}
    with deleting(root) as (env, _, service, _repository, storage):
        pending = storage.preflight()
        assert len(pending) == 1 and pending[0].command == cmd
        with pytest.raises(FileInUseError):
            env.attachments.coordinator.assert_accessible(source.relative_path)
        report = service.recover(pending)
        assert report.recovered == 1 and report.pending == 0 and not report.warnings
        assert not storage.has_pending()
        env.attachments.coordinator.assert_accessible(source.relative_path)
        with env.sessions.transaction() as session:
            assert (session.get(AttachmentRecord, source_id) is None) == committed
            assert (session.get(FileAssetRecord, asset_id) is None) == committed
            assert (session.get(FileOperationReceiptRecord, cmd.job_id) is not None) == committed
        if committed:
            assert not path.exists() and service.execute(cmd).already_committed
        else:
            assert path.read_bytes() == b"initial"
        if extra is not None:
            extra_path = env.attachments.storage_root / extra.relative_path
            assert extra_path.exists() is not committed
            with env.sessions.transaction() as session:
                assert (session.get(AttachmentRecord, extra.id) is None) == committed
            if not committed:
                assert extra_path.read_bytes() == b"second fixture"
        if not committed:
            assert not service.execute(cmd).pending


def test_restore_collision_keeps_both_files_and_protects_only_affected_path(tmp_path):
    with deleting(tmp_path) as (env, _, service, repository, storage):
        source = seed(env)
        original = env.attachments.path_for_open(source.id)
        cmd = request(service, "attachment", source.id)

        def collision(stage):
            if stage == "moved":
                original.write_bytes(b"another file")
                raise RuntimeError("interrupt")

        storage._checkpoint = collision
        with pytest.raises(AttachmentOperationError, match="보존"):
            service.execute(cmd)
        assert original.read_bytes() == b"another file"
        assert (journal_folder(env, cmd) / source.asset_id).read_bytes() == b"initial"
        assert repository.committed(cmd) is None
        pending = storage.preflight()
        assert service.recover(pending).pending == 1
        env.tasks.create(TaskDraft(title="무관한 업무"))
        with pytest.raises(FileInUseError):
            env.attachments.coordinator.assert_accessible(source.relative_path)


def test_post_commit_failure_leaves_pending_asset_and_restart_finishes_without_new_logical_rows(
    tmp_path, monkeypatch
):
    import officeflow.infrastructure.attachments.deletion_storage as storage_module

    with deleting(tmp_path) as (env, _, service, repository, storage):
        source = seed(env)
        cmd = request(service, "attachment", source.id)
        dispose = storage_module.delete_open_file
        monkeypatch.setattr(
            storage_module,
            "delete_open_file",
            lambda _stream: (_ for _ in ()).throw(OSError("deny purge")),
        )
        result = service.execute(cmd)
        assert result.pending and result.warning and repository.committed(cmd) is not None
        with env.sessions.transaction() as session:
            assert session.get(AttachmentRecord, source.id) is None
            assert session.get(FileAssetRecord, source.asset_id).state == "pending_delete"
        monkeypatch.setattr(storage_module, "delete_open_file", dispose)
        assert service.recover(storage.preflight()).pending == 0
        assert service.execute(cmd).already_committed


def test_malformed_preflight_protects_files_without_database_or_hash(tmp_path, monkeypatch):
    with deleting(tmp_path) as (env, _, service, _, storage):
        source = seed(env)
        folder = env.attachments.storage_root / ".trash" / ("delete-" + uuid4().hex)
        folder.mkdir(parents=True)
        (folder / "delete-plan.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            service._repository, "plan", lambda _target: pytest.fail("must not query DB")
        )
        pending = storage.preflight()
        assert pending[0].command is None
        with pytest.raises(FileInUseError):
            env.attachments.coordinator.assert_accessible(source.relative_path)
        assert service.recover(pending).pending == 1


def test_pending_delete_not_consumed_by_legacy_recovery_or_omitted_from_backup(tmp_path):
    paths = AppPaths(tmp_path)
    paths.ensure_directories()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        storage = DeletionFileStorage(
            env.attachments.storage_root, coordinator=env.attachments.coordinator
        )
        repository = SqlAlchemyDeletionRepository(env.sessions)
        service = AttachmentDeletionService(repository, storage)
        assert env.attachments.storage_root == paths.attachment_dir
        source = seed(env)
        cmd = request(service, "attachment", source.id)
        plan = repository.plan(cmd.target)
        storage.prepare(cmd, plan, lambda: False)
        manager = BackupManager(paths, coordinator=env.attachments.coordinator)
        manager.recover_attachment_deletions()
        assert (journal_folder(env, cmd) / source.asset_id).read_bytes() == b"initial"
        backup = manager.create_backup()
        manifest = manager.verify_backup(backup.path)
        assert manifest.recovery_partition
        assert any(
            item.path == source.relative_path and item.category == "asset"
            for item in manifest.attachments
        )
        assert any(
            item.path.startswith(".trash/delete-") and item.category == "recovery"
            for item in manifest.attachments
        )
        assert service.recover(storage.preflight()).recovered == 1


def test_multiple_file_io_outside_write_transaction_and_short_gate_keeps_other_work_available(
    tmp_path,
):
    with deleting(tmp_path) as (env, _, service, _, storage):
        source = seed(env)
        cmd = request(service, "attachment", source.id)
        entered, release, finished = Event(), Event(), Event()
        failures = []

        def checkpoint(stage):
            if stage == "prepared":
                entered.set()
                assert release.wait(10)

        storage._checkpoint = checkpoint

        def execute():
            try:
                service.execute(cmd)
            except BaseException as error:
                failures.append(error)
            finally:
                finished.set()

        worker = Thread(target=execute)
        worker.start()
        try:
            assert entered.wait(10)
            assert env.attachments.coordinator.gate.acquire(timeout=1)
            env.attachments.coordinator.gate.release()
            with env.sessions.transaction() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                assert session.get(TaskRecord, source.task_id) is not None
        finally:
            release.set()
            worker.join(15)
        assert finished.is_set() and not failures


def test_quarantine_handle_kept_through_database_commit(tmp_path):
    with deleting(tmp_path) as (env, _, service, _, storage):
        source = seed(env)
        cmd = request(service, "attachment", source.id)

        def check(stage):
            if stage == "commit_guard":
                target = journal_folder(env, cmd) / source.asset_id
                with pytest.raises(OSError):
                    target.write_bytes(b"cannot replace during commit")
                with pytest.raises(OSError):
                    target.unlink()

        storage._checkpoint = check
        assert not service.execute(cmd).pending


def test_missing_file_and_task_without_files_can_be_explicitly_deleted(tmp_path):
    with deleting(tmp_path) as (env, _, service, _, storage):
        source = seed(env)
        env.attachments.path_for_open(source.id).unlink()
        assert not service.execute(request(service, "attachment", source.id)).pending
        env.tasks.move_to_trash(source.task_id)
        assert not service.execute(request(service, "task", source.task_id)).pending
        assert not storage.has_pending()


def test_hardlinked_source_is_not_moved_or_deleted(tmp_path):
    with deleting(tmp_path) as (env, _, service, _, _):
        source = seed(env)
        path = env.attachments.path_for_open(source.id)
        alias = tmp_path / "outside-alias.txt"
        os.link(path, alias)
        with pytest.raises(AttachmentOperationError, match="독립"):
            service.execute(request(service, "attachment", source.id))
        assert path.read_bytes() == alias.read_bytes() == b"initial"
        assert env.attachments.get_metadata(source.id)


def test_unrecognized_sidecar_preserved_then_metadata_cleanup_can_resume(tmp_path):
    with deleting(tmp_path) as (env, _, service, _, storage):
        source = seed(env)
        cmd = request(service, "attachment", source.id)

        def extra(stage):
            if stage == "committed":
                (journal_folder(env, cmd) / "unknown.txt").write_bytes(b"must preserve")

        storage._checkpoint = extra
        result = service.execute(cmd)
        assert result.pending
        assert (journal_folder(env, cmd) / "unknown.txt").read_bytes() == b"must preserve"
        assert service.recover(storage.preflight()).pending == 1
        # Explicit fixture resolution; the application never deletes this file.
        (journal_folder(env, cmd) / "unknown.txt").rename(tmp_path / "kept-unknown.txt")
        assert service.recover(storage.preflight()).pending == 0
        assert (tmp_path / "kept-unknown.txt").read_bytes() == b"must preserve"


def test_pending_asset_cannot_acquire_new_content_references(tmp_path, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    import officeflow.infrastructure.attachments.deletion_storage as module

    with deleting(tmp_path) as (env, creation, service, _, _):
        source = seed(env)
        creation.execute(command(source, DocumentOperation.ORIGINAL))
        source = env.attachments.get_metadata(source.id)
        monkeypatch.setattr(
            module, "delete_open_file", lambda _stream: (_ for _ in ()).throw(OSError("deny"))
        )
        assert service.execute(request(service, "attachment", source.id)).pending
        with pytest.raises(IntegrityError):
            add_shared_version(env, source)


def test_plan_written_once_and_preflight_does_not_read_per_file_state(tmp_path, monkeypatch):
    with deleting(tmp_path) as (env, _, service, repository, storage):
        source = seed(env)
        for index in range(11):
            file = tmp_path / f"file-{index}.txt"
            file.write_bytes(str(index).encode())
            env.attachments.attach(source.task_id, file)
        env.tasks.move_to_trash(source.task_id)
        cmd = request(service, "task", source.task_id)
        calls = []
        write = storage._write

        def count(*args):
            calls.append(True)
            return write(*args)

        monkeypatch.setattr(storage, "_write", count)
        storage.prepare(cmd, repository.plan(cmd.target), lambda: False)
        assert len(calls) == 1
        with monkeypatch.context() as guard:
            guard.setattr(
                storage,
                "_read_entry",
                lambda *_args: pytest.fail("preflight must not read per-file states"),
            )
            pending = storage.preflight()
        assert len(pending) == 1
        assert service.recover(pending).recovered == 1
        assert len(env.attachments.attachment_metadata_for_task(source.task_id)) == 12


def test_preflight_overflow_is_fail_closed_and_limits_journal_reads(tmp_path, monkeypatch):
    with deleting(tmp_path) as (env, _, _, _, storage):
        root = env.attachments.storage_root / ".trash"
        root.mkdir(parents=True)
        for _ in range(4):
            (root / ("delete-" + uuid4().hex)).mkdir()
        calls = []
        read = storage._read

        def count(*args, **kwargs):
            calls.append(True)
            return read(*args, **kwargs)

        monkeypatch.setattr(storage, "_read", count)
        items = storage.preflight(limit=2)
        assert len(calls) == 2 and len(items) == 3 and items[-1].job_id == "overflow"
        with pytest.raises(FileInUseError):
            env.attachments.coordinator.assert_accessible("unrelated.txt")


def test_missing_delete_journal_cannot_hide_pending_assets_from_complete_backup(
    tmp_path, monkeypatch
):
    import officeflow.infrastructure.attachments.deletion_storage as module

    paths = AppPaths(tmp_path)
    paths.ensure_directories()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        source = seed(env)
        storage = DeletionFileStorage(paths.attachment_dir, coordinator=env.attachments.coordinator)
        service = AttachmentDeletionService(SqlAlchemyDeletionRepository(env.sessions), storage)
        cmd = request(service, "attachment", source.id)
        monkeypatch.setattr(
            module, "delete_open_file", lambda _stream: (_ for _ in ()).throw(OSError("deny"))
        )
        assert service.execute(cmd).pending
        journal_folder(env, cmd).rename(tmp_path / "preserved-job")
        assert not storage.has_pending()
        with pytest.raises(BackupError, match="삭제 후처리"):
            BackupManager(paths, coordinator=env.attachments.coordinator).create_backup()
        assert (tmp_path / "preserved-job" / source.asset_id).read_bytes() == b"initial"
