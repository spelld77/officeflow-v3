import os
from datetime import UTC, datetime

import pytest

from officeflow.application.attachment_export import (
    AttachmentExportService,
    ExportStatus,
    FileReference,
)
from officeflow.application.attachments import AttachmentService, DuplicateAttachmentError
from officeflow.application.file_operations import FileInUseError
from officeflow.application.tasks import TaskDraft, TaskService
from officeflow.domain.attachment import AttachmentCheckStatus
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
from officeflow.infrastructure.attachments.storage import ManagedAttachmentStorage
from officeflow.infrastructure.database.attachment_repository import SqlAlchemyAttachmentRepository
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository


@pytest.fixture
def files(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    database = data / "officeflow.db"
    upgrade_database(database)
    engine = create_database_engine(database)
    sessions = SessionFactory(engine)
    tasks = TaskService(SqlAlchemyTaskRepository(sessions))
    repository = SqlAlchemyAttachmentRepository(sessions)
    managed = ManagedAttachmentStorage(data / "attachments")
    attachments = AttachmentService(repository, managed, tasks)
    storage = ExternalExportStorage(data)
    exports = AttachmentExportService(attachments, storage)
    task = tasks.create(TaskDraft(title="파일 작업"))
    folder = tmp_path / "exports"
    folder.mkdir()
    yield tasks, task, attachments, repository, managed, storage, exports, folder
    engine.dispose()


def attach(files, tmp_path, name="자료.txt", content=b"current"):
    _tasks, task, attachments, *_rest = files
    source = tmp_path / name
    source.write_bytes(content)
    return attachments.attach(task.id, source)


def test_single_save_is_independent_and_uses_current_content(files, tmp_path):
    attached = attach(files, tmp_path)
    _tasks, _task, _attachments, repo, managed, _storage, exports, folder = files
    path = managed.resolve(attached.relative_path)
    path.write_bytes(b"edited and saved")
    result = exports.export(
        (FileReference.attachment(attached.id),), folder / "사본.txt", single=True
    )
    assert result.items[0].status is ExportStatus.SAVED
    output = result.items[0].output_path
    assert output.read_bytes() == b"edited and saved"
    output.write_bytes(b"external edit")
    assert path.read_bytes() == b"edited and saved"
    observed = repo.get_attachment(attached.id)
    assert observed.checksum == attached.checksum
    assert observed.size_bytes == attached.size_bytes
    assert observed.observed_size_bytes == len(b"edited and saved")
    assert observed.last_check_status is AttachmentCheckStatus.MODIFIED
    assert not files[5].pending_operations()


def test_duplicate_checks_current_not_initial_content(files, tmp_path):
    attached = attach(files, tmp_path, content=b"A")
    _tasks, task, attachments, _repo, managed, *_rest = files
    managed.resolve(attached.relative_path).write_bytes(b"B")
    source_a = tmp_path / "new-a.txt"
    source_a.write_bytes(b"A")
    added = attachments.attach(task.id, source_a)
    assert added.id != attached.id
    source_b = tmp_path / "new-b.txt"
    source_b.write_bytes(b"B")
    with pytest.raises(DuplicateAttachmentError):
        attachments.attach(task.id, source_b)


def test_no_baseline_stays_unknown_and_stale_observation_cannot_overwrite(files, tmp_path):
    attached = attach(files, tmp_path)
    _tasks, _task, attachments, repo, managed, *_rest = files
    with repo._sessions.transaction() as session:
        from officeflow.infrastructure.database.models import AttachmentRecord

        record = session.get(AttachmentRecord, attached.id)
        record.checksum = None
    stale = repo.get_attachment(attached.id)
    result = attachments.verify(attached.id)
    assert result.attachment.checksum is None
    assert result.attachment.last_check_status is AttachmentCheckStatus.NO_BASELINE
    sample = managed.observe(attached.relative_path)
    assert (
        repo.update_observation_if_current(
            stale, sample, AttachmentCheckStatus.UNCHANGED, datetime.now(UTC)
        )
        is None
    )


def test_batch_names_never_overwrite_even_case_collisions(files, tmp_path):
    first = attach(files, tmp_path, name="Report.txt", content=b"first")
    second = attach(files, tmp_path, name="report.txt", content=b"second")
    *_, exports, folder = files
    (folder / "REPORT.txt").write_bytes(b"existing")
    result = exports.export(
        (FileReference.attachment(first.id), FileReference.attachment(second.id)), folder
    )
    assert [item.status for item in result.items] == [ExportStatus.SAVED, ExportStatus.SAVED]
    assert (folder / "REPORT.txt").read_bytes() == b"existing"
    assert {item.output_path.name.casefold() for item in result.items} == {
        "report (1).txt",
        "report (2).txt",
    }


@pytest.mark.parametrize("accept", [True, False])
def test_overwrite_requires_consent_and_keeps_old_on_cancel(files, tmp_path, accept):
    attached = attach(files, tmp_path)
    *_, storage, exports, folder = files
    destination = folder / "자료.txt"
    destination.write_bytes(b"old")
    prompts = []
    result = exports.export(
        (FileReference.attachment(attached.id),),
        destination,
        single=True,
        confirm_overwrite=lambda prompt: prompts.append(prompt) or accept,
    )
    assert len(prompts) == 1
    assert destination.read_bytes() == (b"current" if accept else b"old")
    assert result.items[0].status is (ExportStatus.SAVED if accept else ExportStatus.UNPROCESSED)
    assert not storage.pending_operations()


def test_overwrite_changed_target_requires_reconfirmation(files, tmp_path):
    attached = attach(files, tmp_path)
    *_, exports, folder = files
    destination = folder / "자료.txt"
    destination.write_bytes(b"old")
    prompts = []

    def consent(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            destination.write_bytes(b"new user data")
            return True
        return False

    result = exports.export(
        (FileReference.attachment(attached.id),),
        destination,
        single=True,
        confirm_overwrite=consent,
    )
    assert len(prompts) == 2 and prompts[1].changed
    assert destination.read_bytes() == b"new user data"
    assert result.items[0].status is ExportStatus.UNPROCESSED


def test_batch_partial_failure_retains_success_and_retry_only_failure(files, tmp_path):
    first = attach(files, tmp_path, content=b"first")
    second = attach(files, tmp_path, name="second.txt", content=b"second")
    _tasks, _task, _attachments, _repo, managed, _storage, exports, folder = files
    managed.resolve(second.relative_path).unlink()
    refs = (FileReference.attachment(first.id), FileReference.attachment(second.id))
    result = exports.export(refs, folder)
    assert [item.status for item in result.items] == [ExportStatus.SAVED, ExportStatus.FAILED]
    assert result.retry_references == (refs[1],)
    assert result.items[0].output_path.is_file()


def test_delete_pins_protect_attachment_and_task_before_db_cascade(files, tmp_path):
    attached = attach(files, tmp_path)
    tasks, task, attachments, repo, managed, *_rest = files
    tasks.move_to_trash(task.id)
    with attachments.coordinator.pin(attached.relative_path):
        with pytest.raises(FileInUseError):
            attachments.delete_file(attached.id)
        with pytest.raises(FileInUseError):
            tasks.delete_permanently(task.id)
        assert repo.get_attachment(attached.id) is not None
        assert tasks.get_including_deleted(task.id).deleted_at is not None
    assert managed.resolve(attached.relative_path).is_file()


def test_detached_trash_and_orphan_export_do_not_restore_or_delete(files, tmp_path):
    attached = attach(files, tmp_path)
    tasks, task, attachments, repo, managed, _storage, exports, folder = files
    attachments.unlink(attached.id)
    tasks.move_to_trash(task.id)
    result = exports.export(
        (FileReference.attachment(attached.id),), folder / "해제.txt", single=True
    )
    assert result.items[0].status is ExportStatus.SAVED
    assert repo.get_attachment(attached.id).detached_at is not None
    source = tmp_path / "고아.txt"
    source.write_bytes(b"orphan")
    orphan = managed.import_file(source)
    orphan_result = exports.export(
        (FileReference.orphan(orphan.relative_path),), folder / "고아사본.txt", single=True
    )
    assert orphan_result.items[0].status is ExportStatus.SAVED
    assert managed.resolve(orphan.relative_path).read_bytes() == b"orphan"


@pytest.mark.parametrize("kind", ["root", "same", "hardlink"])
def test_forbidden_destinations(files, tmp_path, kind):
    attached = attach(files, tmp_path)
    _tasks, _task, _attachments, _repo, managed, storage, exports, folder = files
    source = managed.resolve(attached.relative_path)
    target = storage.data_root / "illegal.txt" if kind == "root" else source
    if kind == "hardlink":
        target = folder / "hardlink.txt"
        os.link(source, target)
    result = exports.export((FileReference.attachment(attached.id),), target, single=True)
    assert result.items[0].status is ExportStatus.FAILED
    assert source.read_bytes() == b"current"


def test_metadata_failure_does_not_delete_successful_export(files, tmp_path, monkeypatch):
    attached = attach(files, tmp_path)
    _tasks, _task, attachments, _repo, _managed, _storage, exports, folder = files

    def fail(*_args):
        raise RuntimeError("DB not available")

    monkeypatch.setattr(attachments, "record_observation", fail)
    result = exports.export(
        (FileReference.attachment(attached.id),), folder / "ok.txt", single=True
    )
    assert result.items[0].status is ExportStatus.SAVED
    assert "정보" in result.items[0].message
    assert result.items[0].output_path.read_bytes() == b"current"


def test_new_output_name_race_is_numbered_not_replaced(files, tmp_path, monkeypatch):
    attached = attach(files, tmp_path)
    *_, storage, exports, folder = files
    original = storage._publish_new
    calls = []

    def publish(temporary, destination):
        calls.append(destination)
        if len(calls) == 1:
            destination.write_bytes(b"racing writer")
        original(temporary, destination)

    monkeypatch.setattr(storage, "_publish_new", publish)
    result = exports.export((FileReference.attachment(attached.id),), folder)
    assert result.items[0].status is ExportStatus.SAVED
    assert (folder / attached.original_name).read_bytes() == b"racing writer"
    assert result.items[0].output_path.name == "자료 (1).txt"


def test_windows_read_lease_blocks_writes_and_releases_after_capture(files, tmp_path):
    attached = attach(files, tmp_path)
    _tasks, _task, _attachments, _repo, managed, *_rest = files
    with managed.protected_read(attached.relative_path), pytest.raises(PermissionError):
        managed.resolve(attached.relative_path).write_bytes(b"not allowed")
    managed.resolve(attached.relative_path).write_bytes(b"allowed")


def test_locked_existing_target_is_retryable_and_keeps_old(files, tmp_path):
    attached = attach(files, tmp_path)
    *_, storage, exports, folder = files
    destination = folder / "locked.txt"
    destination.write_bytes(b"old")
    with destination.open("wb") as locked:
        locked.write(b"locked old")
        locked.flush()
        result = exports.export(
            (FileReference.attachment(attached.id),),
            destination,
            single=True,
            confirm_overwrite=lambda _prompt: True,
        )
    assert result.items[0].status is ExportStatus.FAILED
    assert result.retry_references
    assert destination.read_bytes() == b"locked old"
    assert not storage.pending_operations()


def test_partial_replace_restores_old_without_clobbering(files, tmp_path, monkeypatch):
    attached = attach(files, tmp_path)
    *_, storage, exports, folder = files
    destination = folder / "replace.txt"
    destination.write_bytes(b"old")

    def partial(destination, _temporary, backup):
        os.rename(destination, backup)
        raise OSError("injected partial ReplaceFileW")

    monkeypatch.setattr(storage, "_replace", partial)
    result = exports.export(
        (FileReference.attachment(attached.id),),
        destination,
        single=True,
        confirm_overwrite=lambda _prompt: True,
    )
    assert result.items[0].status is ExportStatus.NEEDS_REVIEW
    assert destination.read_bytes() == b"old"
    review = storage.inspect_recovery(lambda: False)[0]
    assert review.can_cleanup
    storage.recover(review, restore=False)
    assert destination.read_bytes() == b"old"
    assert not storage.pending_operations()


def test_partial_replace_does_not_replace_new_racing_file(files, tmp_path, monkeypatch):
    attached = attach(files, tmp_path)
    *_, storage, exports, folder = files
    destination = folder / "replace.txt"
    destination.write_bytes(b"old")

    def partial(destination, _temporary, backup):
        os.rename(destination, backup)
        destination.write_bytes(b"racing user data")
        raise OSError("partial failure with new file")

    monkeypatch.setattr(storage, "_replace", partial)
    result = exports.export(
        (FileReference.attachment(attached.id),),
        destination,
        single=True,
        confirm_overwrite=lambda _prompt: True,
    )
    assert result.items[0].status is ExportStatus.NEEDS_REVIEW
    review = storage.inspect_recovery(lambda: False)[0]
    assert not review.can_cleanup and not review.can_restore
    assert destination.read_bytes() == b"racing user data"
    assert review.backup.read_bytes() == b"old"


def test_successful_replace_journal_failure_is_saved_not_retry(files, tmp_path, monkeypatch):
    attached = attach(files, tmp_path)
    *_, storage, exports, folder = files
    destination = folder / "replace.txt"
    destination.write_bytes(b"old")
    original = storage._write_journal

    def fail_after_publish(prepared, state, **kwargs):
        if state == "published":
            raise OSError("journal unavailable")
        original(prepared, state, **kwargs)

    monkeypatch.setattr(storage, "_write_journal", fail_after_publish)
    result = exports.export(
        (FileReference.attachment(attached.id),),
        destination,
        single=True,
        confirm_overwrite=lambda _prompt: True,
    )
    assert result.items[0].status is ExportStatus.SAVED
    assert not result.retry_references
    assert destination.read_bytes() == b"current"
    assert storage.inspect_recovery(lambda: False)[0].can_cleanup


def test_recovery_rechecks_files_after_user_review(files, tmp_path, monkeypatch):
    attached = attach(files, tmp_path)
    *_, storage, exports, folder = files
    original = storage._finish

    def leave_journal(prepared):
        storage._write_journal(prepared, "published")
        return "cleanup pending"

    monkeypatch.setattr(storage, "_finish", leave_journal)
    result = exports.export(
        (FileReference.attachment(attached.id),), folder / "saved.txt", single=True
    )
    review = storage.inspect_recovery(lambda: False)[0]
    assert review.can_cleanup
    result.items[0].output_path.write_bytes(b"later user change")
    with pytest.raises(Exception, match="바뀌었습니다"):
        storage.recover(review, restore=False)
    assert review.journal.is_file()
    monkeypatch.setattr(storage, "_finish", original)


def test_batch_cancel_retains_completed_file(files, tmp_path):
    first = attach(files, tmp_path)
    second = attach(files, tmp_path, name="second.txt", content=b"second")
    *_, exports, folder = files
    result = exports.export(
        (FileReference.attachment(first.id), FileReference.attachment(second.id)),
        folder,
        cancel_requested=lambda: (folder / first.original_name).is_file(),
    )
    assert [item.status for item in result.items] == [ExportStatus.SAVED, ExportStatus.UNPROCESSED]
    assert result.retry_references == (FileReference.attachment(second.id),)


def test_single_extension_preserved_and_batch_task_scope_enforced(files, tmp_path):
    first = attach(files, tmp_path)
    tasks, _task, attachments, *_rest = files
    *_, exports, folder = files
    result = exports.export(
        (FileReference.attachment(first.id),), folder / "renamed.pdf", single=True
    )
    assert result.items[0].output_path.name == "renamed.pdf.txt"
    other_task = tasks.create(TaskDraft(title="다른 업무"))
    source = tmp_path / "other.txt"
    source.write_bytes(b"other")
    other = attachments.attach(other_task.id, source)
    with pytest.raises(ValueError, match="한 업무"):
        exports.export(
            (FileReference.attachment(first.id), FileReference.attachment(other.id)), folder
        )


def test_capture_holds_source_write_lock_and_delete_pin(files, tmp_path):
    attached = attach(files, tmp_path, content=b"x" * (2 * 1024**2))
    tasks, task, attachments, repo, managed, _storage, exports, folder = files
    checks = []

    def progress(_done, _total):
        if checks:
            return
        tasks.move_to_trash(task.id)
        with pytest.raises(FileInUseError):
            tasks.delete_permanently(task.id)
        with pytest.raises(FileInUseError):
            attachments.delete_file(attached.id)
        with pytest.raises(PermissionError):
            managed.resolve(attached.relative_path).write_bytes(b"changed")
        checks.append(True)

    result = exports.export(
        (FileReference.attachment(attached.id),),
        folder / "locked-source.bin",
        single=True,
        progress=progress,
    )
    assert checks and result.items[0].status is ExportStatus.SAVED
    assert repo.get_attachment(attached.id) is not None
    assert not attachments.coordinator.is_pinned(attached.relative_path)


def test_common_file_queue_wait_can_cancel_without_acquiring_pin(files, tmp_path):
    from threading import Event, Thread
    from time import sleep

    attached = attach(files, tmp_path)
    _tasks, _task, attachments, _repo, _managed, _storage, exports, folder = files
    cancel = Event()
    results = []
    with attachments.coordinator.job():
        thread = Thread(
            target=lambda: results.append(
                exports.export(
                    (FileReference.attachment(attached.id),), folder, cancel_requested=cancel.is_set
                )
            )
        )
        thread.start()
        sleep(0.05)
        cancel.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert results[0].items[0].status is ExportStatus.UNPROCESSED
    assert not attachments.coordinator.is_pinned(attached.relative_path)
    assert not tuple(folder.iterdir())


def test_candidate_updates_notify_once_and_unreadable_is_not_duplicate(
    files, tmp_path, monkeypatch
):
    first = attach(files, tmp_path, name="first.txt", content=b"aaaa")
    attach(files, tmp_path, name="second.txt", content=b"bbbb")
    _tasks, task, attachments, _repo, managed, *_rest = files
    notifications = []
    unsubscribe = attachments.subscribe(notifications.append)
    attach(files, tmp_path, name="third.txt", content=b"cccc")
    assert notifications == [task.id]
    notifications.clear()
    original = managed.protected_read

    def locked(path):
        if path == first.relative_path:
            raise PermissionError("document locked")
        return original(path)

    monkeypatch.setattr(managed, "protected_read", locked)
    added = attach(files, tmp_path, name="fourth.txt", content=b"aaaa")
    assert added.operation_warning
    assert notifications == [task.id]
    unsubscribe()


def test_invalid_journal_never_touches_unrelated_user_file(files, tmp_path):
    import json

    *_, storage, _exports, folder = files
    user_file = folder / "important.txt"
    user_file.write_bytes(b"user data")
    storage.journal_root.mkdir(exist_ok=True)
    journal = storage.journal_root / ("a" * 32 + "-" + "b" * 32 + ".json")
    journal.write_text(
        json.dumps(
            {
                "version": 1,
                "job_id": "a" * 32,
                "temporary": str(user_file),
                "destination": str(folder / "other.txt"),
            }
        ),
        encoding="utf-8",
    )
    review = storage.inspect_recovery(lambda: False)[0]
    assert not review.can_cleanup and not review.can_restore
    with pytest.raises(Exception, match="자동"):
        storage.recover(review, restore=False)
    assert user_file.read_bytes() == b"user data" and journal.is_file()


def test_destination_space_failure_stops_batch_and_keeps_sources(files, tmp_path, monkeypatch):
    import errno
    from pathlib import Path

    first = attach(files, tmp_path)
    second = attach(files, tmp_path, name="second.txt", content=b"second")
    _tasks, _task, _attachments, _repo, managed, storage, exports, folder = files
    original = Path.open

    def no_space(path, mode="r", *args, **kwargs):
        if path.suffix == ".part" and mode == "xb":
            raise OSError(errno.ENOSPC, "test destination full")
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_space)
    result = exports.export(
        (FileReference.attachment(first.id), FileReference.attachment(second.id)), folder
    )
    assert [item.status for item in result.items] == [ExportStatus.FAILED, ExportStatus.UNPROCESSED]
    assert managed.resolve(first.relative_path).read_bytes() == b"current"
    assert managed.resolve(second.relative_path).read_bytes() == b"second"
    assert not storage.pending_operations()


def test_explicit_recovery_restores_only_missing_name_and_cleanup_keeps_it(files, tmp_path):
    import hashlib

    attached = attach(files, tmp_path)
    *_, storage, exports, folder = files
    destination = folder / "recover.txt"
    destination.write_bytes(b"old")
    resolved = exports.resolve(FileReference.attachment(attached.id))
    prepared = storage.prepare(
        resolved, destination, storage.new_job_id(), cancel_requested=lambda: False, progress=None
    )
    backup = prepared.temporary.with_suffix(".previous")
    storage._write_journal(
        prepared, "replacing", backup=backup, original_checksum=hashlib.sha256(b"old").hexdigest()
    )
    os.rename(destination, backup)
    review = storage.inspect_recovery(lambda: False)[0]
    assert review.can_restore
    storage.recover(review, restore=True)
    assert destination.read_bytes() == b"old"
    cleanup = storage.inspect_recovery(lambda: False)[0]
    assert cleanup.can_cleanup
    storage.recover(cleanup, restore=False)
    assert destination.read_bytes() == b"old" and not cleanup.journal.exists()


@pytest.mark.parametrize(
    "stage,overwrite",
    [
        ("copying", False),
        ("publishing", False),
        ("published", False),
        ("replacing", True),
        ("published", True),
    ],
)
def test_real_process_kill_preserves_source_target_and_exact_journal(
    files, tmp_path, stage, overwrite
):
    import subprocess
    import sys
    from time import monotonic, sleep

    attached = attach(files, tmp_path)
    _tasks, _task, _attachments, _repo, managed, storage, _exports, folder = files
    source = managed.resolve(attached.relative_path)
    destination = folder / "forced.txt"
    if overwrite:
        destination.write_bytes(b"old")
    marker = tmp_path / "checkpoint.txt"
    child_code = """
import sys, time
from pathlib import Path
from officeflow.application.attachment_export import FileReference, ResolvedFileReference
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
root, source, destination, marker = map(Path, sys.argv[1:5])
stage = sys.argv[5]
def checkpoint(state, prepared):
    if state == stage and (state != "copying" or prepared.temporary_identity is not None):
        marker.write_text(state, encoding="utf-8")
        while True: time.sleep(0.05)
storage = ExternalExportStorage(root, checkpoint=checkpoint)
resolved = ResolvedFileReference(FileReference.orphan("fixture.txt"), source, source.name, "fixture.txt")
prepared = storage.prepare(resolved, destination, storage.new_job_id(), cancel_requested=lambda: False, progress=None)
storage.publish(prepared, numbered=False, cancel_requested=lambda: False, confirm_overwrite=lambda _: True)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_code,
            str(storage.data_root),
            str(source),
            str(destination),
            str(marker),
            stage,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = monotonic() + 15
        while not marker.exists() and process.poll() is None and monotonic() < deadline:
            sleep(0.02)
        assert marker.is_file(), (
            process.communicate(timeout=2)[1].decode(errors="replace")
            if process.poll() is not None
            else "checkpoint timeout"
        )
        process.kill()  # Only the test's own child process; an actual hard stop.
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    assert source.read_bytes() == b"current"
    if stage == "published":
        assert destination.read_bytes() == b"current"
    elif overwrite:
        assert destination.read_bytes() == b"old"
    else:
        assert not destination.exists()
    review = storage.inspect_recovery(lambda: False)[0]
    assert review.journal.is_file()
    if stage == "copying":
        assert not review.can_cleanup
        assert review.temporary.is_file()
    else:
        assert review.can_cleanup
        storage.recover(review, restore=False)
        assert not review.journal.exists()
        if stage == "published":
            assert destination.read_bytes() == b"current"
