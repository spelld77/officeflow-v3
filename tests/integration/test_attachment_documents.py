"""Internal creation contracts use only isolated profiles and actual Windows handles."""

import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from threading import Event, Thread
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text

from officeflow.application.attachment_documents import (
    AttachmentDocumentService,
    DocumentCommand,
    DocumentOperation,
)
from officeflow.application.attachments import AttachmentOperationError
from officeflow.application.file_operations import FileInUseError, FileOperationCoordinator
from officeflow.application.tasks import TaskDraft
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.attachments.internal_storage import InternalFileStorage
from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.database.attachment_document_repository import (
    SqlAlchemyDocumentRepository,
)
from officeflow.infrastructure.database.models import (
    AttachmentVersionRecord,
    DocumentFamilyRecord,
    FileAssetRecord,
    FileOperationReceiptRecord,
)
from tests.integration.test_attachment_search import search_environment


@contextmanager
def documents(root, *, checkpoint=None, attachment_root=None):
    with search_environment(root, attachment_root=attachment_root) as env:
        storage = InternalFileStorage(
            env.attachments.storage_root,
            coordinator=env.attachments.coordinator,
            checkpoint=checkpoint,
        )
        repository = SqlAlchemyDocumentRepository(env.sessions)
        yield env, AttachmentDocumentService(repository, storage)


def seed(env, content=b"initial"):
    task = env.tasks.create(TaskDraft(title="보존 검증"))
    source = env.database.parent / "서식.txt"
    source.write_bytes(content)
    return env.attachments.attach(task.id, source)


def command(item, operation, **kwargs):
    return DocumentCommand(
        uuid4().hex, operation, "attachment", item.id, item.metadata_revision, **kwargs
    )


def result_path(env, result):
    with env.sessions.transaction() as session:
        asset = session.get(FileAssetRecord, result.asset_id)
        return env.attachments.storage_root / asset.relative_path


def test_original_preserves_previous_edit_path_initial_information_and_metadata(tmp_path):
    with documents(tmp_path) as (env, service):
        original = seed(env)
        old_path = env.attachments.path_for_open(original.id)
        old_path.write_bytes(b"saved before designation")
        original = env.attachments.update_memo(
            original.id, "메모 보존", expected_revision=original.metadata_revision
        )
        request = command(original, DocumentOperation.ORIGINAL)
        result = service.execute(request)
        assert result.warning == ""
        converted = env.attachments.get_metadata(original.id)
        retained = env.attachments.get_metadata(result.retained_attachment_id)
        assert converted.role.value == "original"
        assert converted.name == original.name and converted.memo == "메모 보존"
        assert converted.checksum == original.checksum  # Historical import baseline stays intact.
        assert converted.asset_id != retained.asset_id == original.asset_id
        assert retained.detached_at is not None and retained.relative_path == original.relative_path
        fixed = result_path(env, result)
        assert fixed.read_bytes() == b"saved before designation"
        old_path.write_bytes(b"saved later from former editor")
        assert fixed.read_bytes() == b"saved before designation"
        with pytest.raises(AttachmentOperationError, match="직접 열"):
            env.attachments.path_for_open(original.id)
        again = service.execute(request)
        assert again.already_committed and again.asset_id == result.asset_id and not again.warning
        assert len(env.repository.list_detached()) == 1
        assert not list((env.attachments.storage_root / ".operations").iterdir())


def test_work_versions_and_new_work_from_old_version_are_independent(tmp_path):
    with documents(tmp_path) as (env, service):
        source = seed(env)
        work = service.execute(command(source, DocumentOperation.WORK, filename="작성.txt"))
        work_item = env.attachments.get_metadata(work.attachment_id)
        work_path = result_path(env, work)
        work_path.write_bytes(b"v2")
        v2_request = command(
            work_item, DocumentOperation.VERSION, label="검토 반영", memo="버전 메모"
        )
        v2 = service.execute(v2_request)
        work_path.write_bytes(b"v3")
        v3 = service.execute(command(work_item, DocumentOperation.VERSION, label="검토 반영"))
        request = DocumentCommand(
            uuid4().hex,
            DocumentOperation.WORK,
            "version",
            v2.version_id,
            0,
            filename="이전부터 작성.txt",
        )
        branched = service.execute(request)
        result_path(env, branched).write_bytes(b"new branch")
        assert result_path(env, v2).read_bytes() == b"v2"
        assert result_path(env, v3).read_bytes() == b"v3"
        assert work_path.read_bytes() == b"v3"
        with env.sessions.transaction() as session:
            versions = list(
                session.scalars(
                    select(AttachmentVersionRecord).order_by(AttachmentVersionRecord.id)
                )
            )
            assert [v.suggestion_number for v in versions] == [1, 2]
            assert versions[0].memo == "버전 메모" and versions[1].memo == ""
            assert all(v.original_name == "작성.txt" for v in versions)
        assert env.attachments.get_metadata(branched.attachment_id).memo == ""
        assert env.attachments.get_metadata(branched.attachment_id).family_id == source.family_id
        assert env.attachments.path_for_open(source.id).read_bytes() == b"initial"
        assert service.execute(v2_request).already_committed


def test_command_id_cannot_be_reused_with_other_input_or_after_result_deletion(tmp_path):
    with documents(tmp_path) as (env, service):
        source = seed(env)
        request = command(source, DocumentOperation.WORK, filename="작업.txt")
        result = service.execute(request)
        with pytest.raises(ValueError, match="다른 요청"):
            service.execute(replace(request, filename="다른작업.txt"))
        env.attachments.delete_file(result.attachment_id)
        replay = service.execute(request)
        assert (
            replay.already_committed
            and replay.warning
            and replay.attachment_id == result.attachment_id
        )
        assert len(env.attachments.attachment_metadata_for_task(source.task_id)) == 1


@pytest.mark.parametrize("change", ["name", "memo", "order", "detach", "trash"])
def test_metadata_conflict_during_capture_never_creates_success_or_consumes_number(
    tmp_path, change
):
    with documents(tmp_path) as (env, service):
        source = seed(env)

        def alter(stage):
            if stage != "prepared":
                return
            if change == "name":
                env.attachments.rename_attachment(source.id, "바뀐.txt", expected_revision=0)
            elif change == "memo":
                env.attachments.update_memo(source.id, "바뀐 메모", expected_revision=0)
            elif change == "order":
                with env.sessions.transaction() as session:
                    session.execute(
                        text(
                            "UPDATE attachments SET sort_order=20,metadata_revision=1 WHERE id=:id"
                        ),
                        {"id": source.id},
                    )
            elif change == "detach":
                env.attachments.unlink(source.id)
            else:
                env.tasks.move_to_trash(source.task_id)

        service.storage._checkpoint = alter
        with pytest.raises((ValueError, AttachmentOperationError)):
            service.execute(command(source, DocumentOperation.VERSION, label="실패 보관"))
        with env.sessions.transaction() as session:
            assert session.get(DocumentFamilyRecord, source.family_id).next_version_number == 1
            assert list(session.scalars(select(FileOperationReceiptRecord))) == []
            assert list(session.scalars(select(AttachmentVersionRecord))) == []
        assert (env.attachments.storage_root / source.relative_path).read_bytes() == b"initial"
        assert list((env.attachments.storage_root / ".operations").iterdir()) == []


def test_source_handle_blocks_external_writes_and_pin_blocks_app_delete(tmp_path):
    with documents(tmp_path) as (env, service):
        source = seed(env)
        path = env.attachments.path_for_open(source.id)
        checked = []

        def check(stage):
            if stage == "prepared":
                with pytest.raises(OSError):
                    path.write_bytes(b"writer")
                with pytest.raises(FileInUseError):
                    env.attachments.delete_file(source.id)
                checked.append(stage)

        service.storage._checkpoint = check
        assert not service.execute(
            command(source, DocumentOperation.WORK, filename="작업.txt")
        ).warning
        assert checked == ["prepared"]
        path.write_bytes(b"writer after commit")
        assert not env.attachments.coordinator.is_pinned(source.relative_path)


@pytest.mark.parametrize(
    "boundary",
    [
        "journal",
        "copy_chunk",
        "prepared",
        "published_before_journal",
        "published",
        "before_commit",
        "committed",
    ],
)
@pytest.mark.parametrize("operation", ["create_version", "release_original"])
def test_actual_process_exit_has_unambiguous_receipt_and_protected_pending_range(
    tmp_path, boundary, operation
):
    with documents(tmp_path) as (env, creation):
        source = seed(env, b"unchanged source" * 1000)
        if operation == "release_original":
            creation.execute(command(source, DocumentOperation.ORIGINAL))
            source = env.attachments.get_metadata(source.id)
        request = command(
            source,
            DocumentOperation(operation),
            label="강제 종료" if operation == "create_version" else "",
        )
        database, root = env.database, env.attachments.storage_root
    code = """
import json, os, sys
from pathlib import Path
from sqlalchemy import Engine, event
from officeflow.application.attachment_documents import *
from officeflow.application.file_operations import FileOperationCoordinator
from officeflow.infrastructure.attachments.internal_storage import InternalFileStorage
from officeflow.infrastructure.database.attachment_document_repository import SqlAlchemyDocumentRepository
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
raw = json.loads(sys.argv[3])
request = DocumentCommand(**(raw | {'operation': DocumentOperation(raw['operation'])}))
engine = create_database_engine(Path(sys.argv[1]))
if sys.argv[4] == 'before_commit':
    writing = [False]
    def wrote(conn, cursor, statement, parameters, context, many):
        if statement.startswith('INSERT INTO file_operation_receipts'): writing[0] = True
    def before_commit(conn):
        if writing[0]: os._exit(74)
    event.listen(engine, 'after_cursor_execute', wrote)
    event.listen(engine, 'commit', before_commit)
def checkpoint(stage):
    if stage == sys.argv[4]: os._exit(74)
service = AttachmentDocumentService(SqlAlchemyDocumentRepository(SessionFactory(engine)), InternalFileStorage(Path(sys.argv[2]), coordinator=FileOperationCoordinator(), checkpoint=checkpoint))
service.execute(request)
"""
    payload = json.dumps(
        {
            "job_id": request.job_id,
            "operation": request.operation.value,
            "source_kind": request.source_kind,
            "source_id": request.source_id,
            "expected_revision": request.expected_revision,
            "label": request.label,
        }
    )
    process = subprocess.run(
        [sys.executable, "-c", code, str(database), str(root), payload, boundary],
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 74, process.stderr.decode(errors="replace")
    with documents(tmp_path) as (env, service):
        pending = service.storage.preflight()
        assert len(pending) == 1 and pending[0].command == request
        with pytest.raises(FileInUseError), service.coordinator.pin(source.relative_path):
            pytest.fail("protected source should not be used before recovery")
        receipt = service.repository.committed(request)
        if boundary == "committed":
            assert receipt is not None
            replay = service.execute(request)
            assert replay.already_committed and not replay.warning
            assert result_path(env, replay).read_bytes() == b"unchanged source" * 1000
            if operation == "release_original":
                assert env.attachments.get_metadata(source.id).role.value == "general"
                with env.sessions.transaction() as session:
                    kept = session.get(AttachmentVersionRecord, replay.retained_version_id)
                    assert kept.asset_id == source.asset_id
                    assert len(list(session.scalars(select(AttachmentVersionRecord)))) == 1
            with service.coordinator.pin(source.relative_path):
                pass
            assert service.storage.preflight() == ()
        else:
            assert receipt is None
            assert (root / source.relative_path).read_bytes() == b"unchanged source" * 1000
            if operation == "release_original":
                assert env.attachments.get_metadata(source.id).role.value == "original"
            with pytest.raises(FileInUseError):
                service.execute(request)
            with env.sessions.transaction() as session:
                assert session.get(DocumentFamilyRecord, source.family_id).next_version_number == 1
                assert list(session.scalars(select(AttachmentVersionRecord))) == []


def test_copy_runs_without_gate_or_write_transaction_and_queue_cancellation_releases_slot(tmp_path):
    reached, release = Event(), Event()

    def block(stage):
        if stage == "copy_chunk":
            reached.set()
            assert release.wait(10)

    with documents(tmp_path, checkpoint=block) as (env, service):
        source = seed(env)
        outcomes = []
        worker = Thread(
            target=lambda: outcomes.append(
                service.execute(command(source, DocumentOperation.WORK, filename="작업.txt"))
            )
        )
        worker.start()
        try:
            assert reached.wait(10)
            with service.coordinator.gate:
                task = env.tasks.create(TaskDraft(title="복사 중에도 입력"))
                assert task.id
            with pytest.raises(AttachmentOperationError, match="취소"):
                service.execute(
                    command(source, DocumentOperation.WORK, filename="취소.txt"),
                    cancel_requested=lambda: True,
                )
        finally:
            release.set()
            worker.join(10)
        assert not worker.is_alive() and len(outcomes) == 1
        assert not service.coordinator.is_pinned(source.relative_path)


def test_frozen_mismatch_and_hardlink_source_fail_closed(tmp_path):
    with documents(tmp_path) as (env, service):
        source = seed(env)
        fixed = service.execute(command(source, DocumentOperation.ORIGINAL))
        original = env.attachments.get_metadata(source.id)
        result_path(env, fixed).write_bytes(b"externally changed frozen")
        with pytest.raises(AttachmentOperationError, match="보관 후"):
            service.execute(command(original, DocumentOperation.WORK, filename="거절.txt"))
        assert original.fixed_checksum == hashlib.sha256(b"initial").hexdigest()
    with documents(tmp_path / "link") as (env, service):
        source = seed(env)
        path = env.attachments.path_for_open(source.id)
        os.link(path, tmp_path / "second-link.txt")
        with pytest.raises(AttachmentOperationError, match="독립"):
            service.execute(command(source, DocumentOperation.WORK, filename="거절.txt"))
        assert path.read_bytes() == b"initial"


def test_preflight_does_not_read_database_hash_or_all_asset_paths_and_preserves_corrupt_scope(
    tmp_path, monkeypatch
):
    coordinator = FileOperationCoordinator()
    storage = InternalFileStorage(tmp_path, coordinator=coordinator)
    assert storage.preflight() == ()
    bad = tmp_path / ".operations" / uuid4().hex
    bad.mkdir(parents=True)
    (bad / "journal.json").write_text('{"version":999}', encoding="utf-8")
    monkeypatch.setattr(
        storage, "_digest", lambda *args: pytest.fail("preflight must not hash files")
    )
    assert "보존" in storage.preflight()[0].message
    with pytest.raises(FileInUseError), coordinator.pin("unrelated/file.txt"):
        pass
    assert (bad / "journal.json").is_file()


def test_exclusive_deletion_intent_rejects_new_pin_and_conflicting_intent(tmp_path):
    coordinator = FileOperationCoordinator()
    with (
        coordinator.pin("a.txt"),
        pytest.raises(FileInUseError),
        coordinator.deletion_intent(("a.txt",), owner="delete"),
    ):
        pass
    with coordinator.deletion_intent(("a.txt",), owner="delete"):
        with pytest.raises(FileInUseError), coordinator.pin("A.txt"):
            pass
        with pytest.raises(FileInUseError), coordinator.deletion_intent(("a.txt",), owner="other"):
            pass
        with coordinator.pin("b.txt"):
            pass
    with coordinator.pin("a.txt"):
        pass


def test_actual_created_documents_survive_complete_backup_and_new_profile_restore(tmp_path):
    paths = AppPaths(tmp_path / "source-profile")
    paths.ensure_directories()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        storage = InternalFileStorage(paths.attachment_dir, coordinator=env.attachments.coordinator)
        service = AttachmentDocumentService(SqlAlchemyDocumentRepository(env.sessions), storage)
        source = seed(env)
        work = service.execute(command(source, DocumentOperation.WORK, filename="작성.txt"))
        work_item = env.attachments.get_metadata(work.attachment_id)
        result_path(env, work).write_bytes(b"saved version")
        version = service.execute(
            command(work_item, DocumentOperation.VERSION, label="제출본", memo="버전 메모")
        )
        original = service.execute(command(source, DocumentOperation.ORIGINAL))
        renamed = env.attachments.rename_attachment(
            work_item.id, "이름 변경.txt", expected_revision=0
        )
        assert renamed.name == "이름 변경.txt"
        with env.sessions.transaction() as session:
            row = session.get(AttachmentVersionRecord, version.version_id)
            assert (
                row.original_name == "작성.txt"
                and json.loads(row.source_payload)["name"] == "작성.txt"
            )
        manager = BackupManager(paths, coordinator=env.attachments.coordinator)
        backup = manager.create_backup()
        manifest = manager.verify_backup(backup.path)
        assert manifest.format_version == 3
        assert {v.asset_id for v in manifest.attachments if v.category == "asset"} == {
            source.asset_id,
            work.asset_id,
            version.asset_id,
            original.asset_id,
        }
    destination = AppPaths(tmp_path / "restored-profile")
    destination.ensure_directories()
    with search_environment(destination.data_dir, attachment_root=destination.attachment_dir):
        pass
    restored_manager = BackupManager(destination)
    restored_manager.stage_restore(backup.path)
    restored_manager.apply_pending_restore()
    with search_environment(
        destination.data_dir, attachment_root=destination.attachment_dir
    ) as env:
        assert result_path(env, work).read_bytes() == b"saved version"
        assert result_path(env, version).read_bytes() == b"saved version"
        assert result_path(env, original).read_bytes() == b"initial"
        assert env.attachments.get_metadata(work.attachment_id).name == "이름 변경.txt"
        assert env.attachments.get_metadata(original.retained_attachment_id).detached_at is not None
        receipt = SqlAlchemyDocumentRepository(env.sessions).committed(
            command(source, DocumentOperation.ORIGINAL)
        )
        assert receipt is None  # A different command ID is not an idempotent replay.
        with env.sessions.transaction() as session:
            assert len(list(session.scalars(select(FileOperationReceiptRecord)))) == 3
            assert session.get(AttachmentVersionRecord, version.version_id).memo == "버전 메모"


def test_published_db_failure_is_preserved_protected_not_orphaned_or_silently_backed_up(tmp_path):
    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        source = seed(env)
        service = AttachmentDocumentService(
            SqlAlchemyDocumentRepository(env.sessions),
            InternalFileStorage(paths.attachment_dir, coordinator=env.attachments.coordinator),
        )
        request = command(source, DocumentOperation.ORIGINAL)

        def fail(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.startswith("INSERT INTO file_operation_receipts"):
                raise OSError("synthetic receipt failure")

        event.listen(env.engine, "before_cursor_execute", fail)
        try:
            with pytest.raises(OSError, match="synthetic"):
                service.execute(request)
        finally:
            event.remove(env.engine, "before_cursor_execute", fail)
        assert service.repository.committed(request) is None
        unchanged = env.attachments.get_metadata(source.id)
        assert unchanged.asset_id == source.asset_id and unchanged.role.value == "general"
        assert (paths.attachment_dir / source.relative_path).read_bytes() == b"initial"
        pending = service.storage.preflight()
        assert (
            len(pending) == 1
            and (paths.attachment_dir / pending[0].output_path).read_bytes() == b"initial"
        )
        report = service.recover_confirmed(pending)
        assert report.recovered == 0 and report.pending == 1
        with pytest.raises(FileInUseError):
            env.attachments.path_for_open(source.id)
        manager = BackupManager(paths, coordinator=env.attachments.coordinator)
        assert not manager.list_orphan_attachment_files()
        backup = manager.create_backup()
        manifest = manager.verify_backup(backup.path)
        assert manifest.recovery_partition
        assert any(
            item.path.startswith(".operations/") and item.category == "recovery"
            for item in manifest.attachments
        )
        assert any(
            item.path == pending[0].output_path and item.category == "recovery"
            for item in manifest.attachments
        )
        # Scoped protection must not disable unrelated work/metadata or ordinary files.
        other = seed(env, b"different unrelated source")
        assert env.attachments.path_for_open(other.id).read_bytes() == b"different unrelated source"


def test_locked_source_and_destination_collision_never_overwrite_or_keep_false_success(tmp_path):
    with documents(tmp_path) as (env, service):
        source = seed(env)
        path = env.attachments.path_for_open(source.id)
        with path.open("r+b"), pytest.raises(OSError):
            service.execute(command(source, DocumentOperation.WORK, filename="잠금.txt"))
        assert service.storage.preflight() == ()
        collisions = []

        def collide(stage):
            if stage == "prepared":
                journal = next(
                    (env.attachments.storage_root / ".operations").glob("*/journal.json")
                )
                output = (
                    env.attachments.storage_root
                    / json.loads(journal.read_text(encoding="utf-8"))["output"]
                )
                output.parent.mkdir(parents=True)
                output.write_bytes(b"unrelated collision")
                collisions.append(output)

        service.storage._checkpoint = collide
        request = command(source, DocumentOperation.WORK, filename="충돌.txt")
        with pytest.raises(OSError):
            service.execute(request)
        assert collisions[0].read_bytes() == b"unrelated collision"
        assert path.read_bytes() == b"initial"
        assert service.repository.committed(request) is None
        assert len(service.storage.preflight()) == 1


def test_committed_recovery_clears_only_matching_record_and_keeps_unknown_auxiliary_file(tmp_path):
    def interruption(stage):
        if stage == "committed":
            raise OSError("notification interrupted")

    with documents(tmp_path, checkpoint=interruption) as (env, service):
        source = seed(env)
        result = service.execute(command(source, DocumentOperation.VERSION, label="이미 확정"))
        assert result.warning
        pending = service.storage.preflight()
        folder = env.attachments.storage_root / ".operations" / result.job_id
        unknown = folder / "unknown.txt"
        unknown.write_bytes(b"keep auxiliary material")
        report = service.recover_confirmed(pending)
        assert report.pending == 1 and report.recovered == 0
        assert unknown.read_bytes() == b"keep auxiliary material"
        unknown.unlink()  # Explicit test-fixture resolution, not an app cleanup rule.
        report = service.recover_confirmed(pending)
        assert report.recovered == 1 and report.pending == 0
        assert not folder.exists()
        assert result_path(env, result).read_bytes() == b"initial"


def test_preflight_limit_does_not_materialize_or_parse_remaining_journals(tmp_path, monkeypatch):
    storage = InternalFileStorage(tmp_path, coordinator=FileOperationCoordinator())
    for _ in range(101):
        folder = tmp_path / ".operations" / uuid4().hex
        folder.mkdir(parents=True)
        (folder / "journal.json").write_text("invalid", encoding="utf-8")
    parsed = []

    def read(job_id):
        parsed.append(job_id)
        raise ValueError("synthetic unreadable")

    monkeypatch.setattr(storage, "_read", read)
    pending = storage.preflight(limit=20, budget_seconds=10)
    assert len(parsed) == 20 and len(pending) == 21
    assert "추가" in pending[-1].message
    assert storage.coordinator.is_protected("unknown-file.txt")
