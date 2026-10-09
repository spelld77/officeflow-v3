from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.task_snapshots import SnapshotCommand, SnapshotSelection
from officeflow.application.tasks import TaskDraft
from officeflow.infrastructure.attachments.snapshot_storage import SnapshotStorage
from officeflow.infrastructure.database.document_catalog import DocumentCatalog
from officeflow.infrastructure.database.models import (
    SnapshotFileRecord,
    TaskSnapshotRecord,
)
from officeflow.infrastructure.database.task_snapshot_repository import TaskSnapshotService
from tests.integration.test_attachment_documents import command, documents, seed


def service(env):
    return TaskSnapshotService(
        env.sessions, SnapshotStorage(env.attachments.storage_root, env.attachments.coordinator)
    )


def test_zero_files_saved_information_and_checklist_are_immutable(tmp_path):
    with documents(tmp_path) as (env, _creation):
        task = env.tasks.create(TaskDraft("初", description="予算 100万円"))
        env.records.add_checklist_item(task.id, "項目")
        snapshots = service(env)
        cmd = SnapshotCommand(uuid4().hex, task.id, "検討前")
        result = snapshots.create(cmd)
        env.tasks.update(task.id, TaskDraft("次"))
        detail = snapshots.get(task.id, result.snapshot_id)
        assert detail.info["values"]["title"] == "初" and detail.file_count == 0
        assert detail.info["checklist"][0]["content"] == "項目"
        assert snapshots.create(cmd).already_committed
        assert snapshots.suggestion(task.id) == "업무 보관 2"


def test_mutable_frozen_shared_assets_preserve_after_source_deletion(tmp_path):
    with documents(tmp_path) as (env, creation):
        source = seed(env)
        creation.execute(command(source, DocumentOperation.ORIGINAL))
        original = env.attachments.get_metadata(source.id)
        work = creation.execute(command(original, DocumentOperation.WORK, filename="作業.txt"))
        snapshots = service(env)
        result = snapshots.create(
            SnapshotCommand(
                uuid4().hex,
                source.task_id,
                "確認前",
                (
                    SnapshotSelection("attachment", original.id),
                    SnapshotSelection("attachment", work.attachment_id),
                ),
                documents_closed=True,
            )
        )
        catalog = DocumentCatalog(env.sessions)
        files = catalog.snapshot_files(source.task_id, result.snapshot_id).items
        assert len(files) == 2
        assert files[0].file.asset_id == original.asset_id
        assert files[1].file.asset_id != work.asset_id
        original_path = env.attachments.storage_root / original.relative_path
        env.attachments.delete_file(original.id)
        env.attachments.delete_file(work.attachment_id)
        assert original_path.exists()
        for item in files:
            assert (
                env.attachments.storage_root / item.file.relative_path
            ).read_bytes() == b"initial"


@pytest.mark.parametrize("failure", ["change", "aba", "cancel", "database"])
def test_capture_failure_never_creates_partial_bundle(tmp_path, failure):
    from sqlalchemy import event

    with documents(tmp_path) as (env, _creation):
        source = seed(env)
        snapshots = service(env)
        cancel = [False]

        def checkpoint(stage):
            if stage == "prepared":
                if failure in {"change", "aba"}:
                    old = env.tasks.get(source.task_id)
                    env.tasks.update(source.task_id, TaskDraft("new"))
                    if failure == "aba":
                        env.tasks.update(source.task_id, TaskDraft(old.title))
                if failure == "cancel":
                    cancel[0] = True

        snapshots.storage.checkpoint = checkpoint

        def fail(_mapper, _connection, _target):
            raise OSError("DB failed")

        if failure == "database":
            event.listen(TaskSnapshotRecord, "before_insert", fail)
        cmd = SnapshotCommand(
            uuid4().hex,
            source.task_id,
            "失敗",
            (SnapshotSelection("attachment", source.id),),
            documents_closed=True,
        )
        try:
            with pytest.raises((ValueError, OSError, RuntimeError)):
                snapshots.create(cmd, canceled=lambda: cancel[0])
        finally:
            if failure == "database":
                event.remove(TaskSnapshotRecord, "before_insert", fail)
        with env.sessions.transaction() as session:
            assert session.scalar(select(func.count()).select_from(TaskSnapshotRecord)) == 0
            assert session.scalar(select(func.count()).select_from(SnapshotFileRecord)) == 0
        assert snapshots.storage.folder(cmd.job_id).is_dir()
        assert (env.attachments.storage_root / source.relative_path).read_bytes() == b"initial"


def test_missing_and_other_task_selection_refused_without_bundle(tmp_path):
    with documents(tmp_path) as (env, _creation):
        source = seed(env)
        other = env.tasks.create(TaskDraft("other"))
        snapshots = service(env)
        cmd = SnapshotCommand(
            uuid4().hex,
            other.id,
            "bad",
            (SnapshotSelection("attachment", source.id),),
            documents_closed=True,
        )
        with pytest.raises(ValueError, match="다른 업무"):
            snapshots.create(cmd)
        missing = replace(cmd, job_id=uuid4().hex, task_id=source.task_id)
        (env.attachments.storage_root / source.relative_path).unlink()
        with pytest.raises(OSError):
            snapshots.create(missing)
