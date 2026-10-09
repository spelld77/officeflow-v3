"""Release creates editable bytes without losing the formerly fixed original."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import event

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.attachments import AttachmentCanceledError, AttachmentOperationError
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.database.models import FileOperationReceiptRecord
from tests.integration.test_attachment_documents import command, result_path, seed
from tests.integration.test_document_lifecycle import lifecycle, original


def release(env, facade, item):
    return facade.create(
        facade.catalog.get("attachment", item.id), DocumentOperation("release_original")
    )


def test_release_preserves_identity_fixed_content_metadata_and_modified_view(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        item = env.attachments.rename_attachment(
            item.id, "새 이름.txt", expected_revision=item.metadata_revision
        )
        item = env.attachments.update_memo(
            item.id, "기준 메모", expected_revision=item.metadata_revision
        )
        view = facade.prepare_open("attachment", item.id)
        view.write_bytes(b"edited view")
        work = facade.create(
            facade.catalog.get("attachment", item.id),
            DocumentOperation.WORK,
            filename="existing work.txt",
        )
        before_work = facade.catalog.get("attachment", work.attachment_id)
        request = command(item, DocumentOperation("release_original"))
        result = facade.documents.execute(request)
        current = env.attachments.get_metadata(item.id)
        assert current.role.value == "general" and not current.is_frozen
        assert current.id == item.id and current.asset_id != item.asset_id
        assert (current.name, current.original_name, current.memo, current.sort_order) == (
            item.name,
            item.original_name,
            item.memo,
            item.sort_order,
        )
        assert (current.family_id, current.checksum, current.size_bytes, current.created_at) == (
            item.family_id,
            item.checksum,
            item.size_bytes,
            item.created_at,
        )
        assert current.metadata_revision == item.metadata_revision + 1
        saved = facade.catalog.get("version", result.retained_version_id)
        assert saved.label == "원본 해제 전 내용"
        assert saved.file.asset_id == item.asset_id and saved.memo == item.memo
        assert saved.source_name_at_creation == item.name
        assert result_path(env, result).read_bytes() == b"initial"
        editable = facade.prepare_open("attachment", current.id)
        editable.write_bytes(b"editable later")
        assert (env.attachments.storage_root / item.relative_path).read_bytes() == b"initial"
        assert view.read_bytes() == b"edited view"
        assert facade.catalog.get("attachment", work.attachment_id) == before_work
        replay = facade.documents.execute(request)
        assert replay.already_committed and replay.retained_version_id == saved.id
        assert len(facade.catalog.page(item.task_id)) == 1
        copy = facade.cache.by_path(view)
        closed = facade.cache.confirm_closed(copy, copy.use_token)
        kept = facade.cache.preserve(closed, "view work.txt", facade.documents, lambda: False)
        assert result_path(env, kept).read_bytes() == b"edited view"
        facade.delete(facade.plan("attachment", item.id))
        assert facade.prepare_open("version", saved.id).read_bytes() == b"initial"


@pytest.mark.parametrize(
    "case", ["general", "stale", "trash", "detached", "corrupt", "lock", "cancel", "db"]
)
def test_release_failure_preserves_original_and_creates_no_retained_version(tmp_path, case):
    with lifecycle(tmp_path) as (env, facade):
        item = seed(env) if case == "general" else original(env, facade)
        operation = DocumentOperation("release_original")
        request = command(item, operation)
        path = env.attachments.storage_root / item.relative_path
        if case == "stale":
            env.attachments.update_memo(item.id, "new", expected_revision=item.metadata_revision)
        elif case == "trash":
            env.tasks.move_to_trash(item.task_id)
        elif case == "detached":
            env.attachments.unlink(item.id)
        elif case == "corrupt":
            path.write_bytes(b"unexpected")
        initial = path.read_bytes()
        lock = path.open("r+b") if case == "lock" else None
        if lock:
            lock.__enter__()
        engine = env.engine

        def fail(conn, cursor, statement, parameters, context, many):
            if statement.startswith("INSERT INTO file_operation_receipts"):
                raise RuntimeError("injected release transaction failure")

        if case == "db":
            event.listen(engine, "before_cursor_execute", fail)
        try:
            with pytest.raises(
                (
                    ValueError,
                    AttachmentOperationError,
                    AttachmentCanceledError,
                    OSError,
                    RuntimeError,
                )
            ):
                facade.documents.execute(request, cancel_requested=lambda: case == "cancel")
        finally:
            if lock:
                lock.__exit__(None, None, None)
            if case == "db":
                event.remove(engine, "before_cursor_execute", fail)
        assert path.read_bytes() == initial
        assert env.attachments.get_metadata(item.id).asset_id == item.asset_id
        assert facade.catalog.page(item.task_id) == ()
        with env.sessions.transaction() as session:
            assert session.get(FileOperationReceiptRecord, request.job_id) is None
        if case == "db":
            retry = facade.documents.retry_pending(request, lambda: False)
            assert retry.retained_version_id is not None
            assert env.attachments.get_metadata(item.id).role.value == "general"
            assert len(facade.catalog.page(item.task_id)) == 1
            assert result_path(env, retry).read_bytes() == initial


@pytest.mark.parametrize(
    "operation", [DocumentOperation.ORIGINAL, DocumentOperation.WORK, DocumentOperation.VERSION]
)
def test_receipts_without_new_optional_result_field_remain_readable(tmp_path, operation):
    with lifecycle(tmp_path) as (env, facade):
        item = seed(env)
        request = command(
            item,
            operation,
            filename="작업.txt" if operation is DocumentOperation.WORK else "",
            label="보관" if operation is DocumentOperation.VERSION else "",
        )
        first = facade.documents.execute(request)
        with env.sessions.transaction() as session:
            receipt = session.get(FileOperationReceiptRecord, request.job_id)
            payload = json.loads(receipt.payload)
            del payload["result"]["retained_version_id"]
            receipt.payload = json.dumps(payload)
        replay = facade.documents.execute(request)
        assert replay.already_committed and replay.asset_id == first.asset_id
        assert not replay.warning and replay.retained_version_id is None


def test_release_backup_restores_current_and_retained_original(tmp_path):
    paths = AppPaths(tmp_path / "source-profile")
    paths.ensure_directories()
    with lifecycle(paths.data_dir, attachment_root=paths.attachment_dir) as (env, facade):
        item = original(env, facade)
        result = release(env, facade, item)
        result_path(env, result).write_bytes(b"editable current")
        manager = BackupManager(paths, coordinator=env.attachments.coordinator)
        backup = manager.create_backup()
        manifest = manager.verify_backup(backup.path)
        assert manifest.format_version == 3
    restored = AppPaths(tmp_path / "restored-profile")
    restored.ensure_directories()
    with lifecycle(restored.data_dir, attachment_root=restored.attachment_dir):
        pass
    manager = BackupManager(restored)
    manager.stage_restore(backup.path)
    manager.apply_pending_restore()
    with lifecycle(restored.data_dir, attachment_root=restored.attachment_dir) as (env, facade):
        current = env.attachments.get_metadata(item.id)
        assert current.role.value == "general"
        assert facade.prepare_open("attachment", item.id).read_bytes() == b"editable current"
        assert facade.prepare_open("version", result.retained_version_id).read_bytes() == b"initial"


@pytest.mark.parametrize(
    "case",
    ["ok", "changed", "reopened", "unchecked", "sidecar", "cancel", "lock", "old_generation"],
)
def test_discard_after_close_preserves_guards_without_separate_close_action(tmp_path, case):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        view = facade.prepare_open("attachment", item.id)
        view.write_bytes(b"saved edit")
        approved = facade.cache.observe(facade.cache.by_path(view))
        assert not approved.closed
        if case == "changed":
            view.write_bytes(b"unapproved later edit")
        elif case == "reopened":
            # Opening a modified copy creates a different copy; simulate this copy's token change.
            facade.cache._save(replace(approved, use_token="0" * 32))
        elif case == "sidecar":
            (view.parent / "editor.tmp").write_bytes(b"sidecar")
        elif case == "old_generation":
            facade.cache.generation = "a" * 32
        lock = view.open("r+b") if case == "lock" else None
        if case == "ok":
            facade.cache.discard_after_close(approved, closed_consent=True)
            assert not view.exists()
            assert facade.prepare_open("attachment", item.id).read_bytes() == b"initial"
        else:
            try:
                with pytest.raises((ValueError, AttachmentCanceledError, RuntimeError, OSError)):
                    facade.cache.discard_after_close(
                        approved,
                        closed_consent=case != "unchecked",
                        canceled=lambda: case == "cancel",
                    )
            finally:
                if lock:
                    lock.close()
            assert view.exists()
        assert (env.attachments.storage_root / item.relative_path).read_bytes() == b"initial"
