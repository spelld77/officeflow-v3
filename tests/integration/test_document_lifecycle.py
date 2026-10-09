"""Phase-2 workflows against real SQLite/files in an isolated profile."""

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from time import time

import pytest
from sqlalchemy import func, select

from officeflow.application.attachment_deletion import AttachmentDeletionService
from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.attachment_export import (
    AttachmentExportService,
    ExportStatus,
    FileReference,
)
from officeflow.application.document_lifecycle import DocumentLifecycle
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.attachments.deletion_storage import DeletionFileStorage
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
from officeflow.infrastructure.attachments.view_cache import AttachmentViewCache
from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.database.attachment_deletion_repository import (
    SqlAlchemyDeletionRepository,
)
from officeflow.infrastructure.database.document_catalog import DocumentCatalog
from officeflow.infrastructure.database.models import AttachmentRecord, FileOperationReceiptRecord
from tests.integration.test_attachment_documents import documents, seed


@contextmanager
def lifecycle(root, *, attachment_root=None):
    with documents(root, attachment_root=attachment_root) as (env, service):
        cache = AttachmentViewCache(env.attachments.storage_root, env.attachments.coordinator)
        delete = AttachmentDeletionService(
            SqlAlchemyDeletionRepository(env.sessions),
            DeletionFileStorage(
                env.attachments.storage_root, coordinator=env.attachments.coordinator
            ),
        )
        facade = DocumentLifecycle(service, delete, DocumentCatalog(env.sessions), cache)
        env.attachments.lifecycle = facade
        try:
            yield env, facade
        finally:
            cache.close()


def original(env, facade):
    item = seed(env)
    facade.create(facade.catalog.get("attachment", item.id), DocumentOperation.ORIGINAL)
    return env.attachments.get_metadata(item.id)


def test_original_views_are_independent_reused_and_not_counted(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        first = facade.prepare_open("attachment", item.id)
        assert first != env.attachments.storage_root / item.relative_path
        assert first.read_bytes() == b"initial"
        second = facade.prepare_open("attachment", item.id)
        assert second == first
        assert len(env.attachments.attachments_for_task(item.task_id)) == 1
        cache = facade.cache.by_path(first)
        assert not cache.closed and cache.use_token
        facade.cache.confirm_closed(cache, cache.use_token)
        facade.cache.discard(facade.cache.get(cache.generation, cache.cache_id))
        assert not first.exists()
        assert facade.prepare_open("attachment", item.id) != first


def test_edited_view_preserves_bytes_as_work_not_original(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        path = facade.prepare_open("attachment", item.id)
        path.write_bytes(b"edited saved view")
        copy = facade.cache.by_path(path)
        observed = facade.cache.confirm_closed(copy, copy.use_token)
        assert observed.state == "modified"
        result = facade.cache.preserve(observed, "새 작업본.txt", facade.documents, lambda: False)
        work = env.attachments.get_metadata(result.attachment_id)
        assert work.role.value == "work" and work.family_id == item.family_id
        assert (
            env.attachments.storage_root / work.relative_path
        ).read_bytes() == b"edited saved view"
        assert (env.attachments.storage_root / item.relative_path).read_bytes() == b"initial"
        assert not path.exists()
        assert facade.prepare_open("attachment", item.id).read_bytes() == b"initial"


@pytest.mark.parametrize(
    "closed,modified,expired,removed",
    [
        (False, False, True, False),
        (True, True, True, False),
        (True, False, False, False),
        (True, False, True, True),
    ],
)
def test_cleanup_requires_closed_unchanged_and_seven_days(
    tmp_path, closed, modified, expired, removed
):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        path = facade.prepare_open("attachment", item.id)
        copy = facade.cache.by_path(path)
        if modified:
            path.write_bytes(b"saved edit")
        if closed:
            copy = facade.cache.confirm_closed(copy, copy.use_token)
        facade.cache._save(
            replace(copy, last_used=time() - 8 * 86400 if expired else time()), due=0
        )
        assert facade.cache.maintenance() == int(removed)
        assert path.exists() != removed


def test_same_stat_modified_bytes_never_auto_deleted(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        path = facade.prepare_open("attachment", item.id)
        copy = facade.cache.by_path(path)
        copy = facade.cache.confirm_closed(copy, copy.use_token)
        stat = path.stat()
        path.write_bytes(b"changed")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        facade.cache._save(replace(copy, last_used=time() - 8 * 86400), due=0)
        assert facade.cache.maintenance() == 0 and path.read_bytes() == b"changed"


@pytest.mark.parametrize("change", ["edit", "reopen", "sidecar"])
def test_discard_checks_latest_bytes_token_and_unknown_sidecars(tmp_path, change):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        path = facade.prepare_open("attachment", item.id)
        copy = facade.cache.by_path(path)
        copy = facade.cache.confirm_closed(copy, copy.use_token)
        if change == "edit":
            path.write_bytes(b"new content")
        elif change == "reopen":
            facade.prepare_open("attachment", item.id)
        else:
            (path.parent / "editor.tmp").write_bytes(b"unowned")
        with pytest.raises(ValueError):
            facade.cache.discard(copy)
        assert path.exists()


def test_bad_fixed_source_cannot_be_hidden_by_good_cache_and_salvage_does_not_need_source(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        path = facade.prepare_open("attachment", item.id)
        (env.attachments.storage_root / item.relative_path).write_bytes(b"tampered frozen")
        with pytest.raises(RuntimeError, match="보관 후"):
            facade.prepare_open("attachment", item.id)
        path.write_bytes(b"valuable edit")
        copy = facade.cache.by_path(path)
        copy = facade.cache.confirm_closed(copy, copy.use_token)
        result = facade.cache.preserve(copy, "복구 작업본.txt", facade.documents, lambda: False)
        assert (
            env.attachments.storage_root
            / env.attachments.get_metadata(result.attachment_id).relative_path
        ).read_bytes() == b"valuable edit"


def test_versions_survive_parent_deletion_and_mixed_save(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = seed(env)
        version = facade.create(
            facade.catalog.get("attachment", item.id),
            DocumentOperation.VERSION,
            label="검토 전",
            memo="내부 보관 메모",
        )
        frozen = facade.catalog.get("version", version.version_id)
        assert frozen.name == item.name and frozen.memo == "내부 보관 메모"
        exports = AttachmentExportService(
            env.attachments, ExternalExportStorage(tmp_path / "export-journals")
        )
        target = tmp_path / "external"
        target.mkdir()
        saved = exports.export(
            (FileReference.attachment(item.id), FileReference.version(frozen.id)), target
        )
        assert all(result.status == ExportStatus.SAVED for result in saved.items)
        assert len(list(target.iterdir())) == 2
        facade.delete(facade.plan("attachment", item.id))
        assert facade.catalog.page(item.task_id)[0].id == frozen.id
        work = facade.create(
            facade.catalog.get("version", frozen.id),
            DocumentOperation.WORK,
            filename="보관에서 재시작.txt",
        )
        assert env.attachments.get_metadata(work.attachment_id).family_id == frozen.family_id
        assert all(result.output_path.exists() for result in saved.items)


def test_corrupt_index_rebuild_is_bounded_and_preserves_json(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        path = facade.prepare_open("attachment", item.id)
        copy = facade.cache.by_path(path)
        facade.cache.close()
        facade.cache.index.write_bytes(b"not sqlite")
        rebuilt = AttachmentViewCache(env.attachments.storage_root, env.attachments.coordinator)
        assert not rebuilt.repair_batch(limit=1)
        for _ in range(10):
            if rebuilt.repair_batch(limit=1):
                break
        assert rebuilt.get(copy.generation, copy.cache_id).relative_path == copy.relative_path
        assert rebuilt.page()[0].cache_id == copy.cache_id and path.exists()
        rebuilt.close()


def test_old_generation_copy_is_never_adopted_or_deleted(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        path = facade.prepare_open("attachment", item.id)
        copy = facade.cache.by_path(path)
        copy = facade.cache.confirm_closed(copy, copy.use_token)
        facade.cache.generation = "a" * 32
        with pytest.raises(ValueError):
            facade.cache.discard(copy)
        with pytest.raises(ValueError):
            facade.cache.preserve(copy, "복원 이전.txt", facade.documents, lambda: False)
        assert path.exists()


@pytest.mark.parametrize("stage", ["prepared", "published_before_journal", "committed"])
def test_cache_preservation_actual_process_death_and_idempotent_retry(tmp_path, stage):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        path = facade.prepare_open("attachment", item.id)
        path.write_bytes(b"edit before crash")
        copy = facade.cache.by_path(path)
        facade.cache.confirm_closed(copy, copy.use_token)
        with env.sessions.transaction() as session:
            before = session.scalar(select(func.count()).select_from(AttachmentRecord))
    script = """
import os,sys
from pathlib import Path
from tests.integration.test_document_lifecycle import lifecycle
with lifecycle(Path(sys.argv[1])) as (env,facade):
    facade.documents.storage._checkpoint = lambda point: os._exit(76) if point==sys.argv[2] else None
    copy = facade.cache.page()[0]
    facade.cache.preserve(copy,'보존 작업본.txt',facade.documents,lambda:False)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), stage],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 76, child.stderr
    with lifecycle(tmp_path) as (env, facade):
        copy = facade.cache.page()[0]
        result = facade.cache.preserve(copy, "보존 작업본.txt", facade.documents, lambda: False)
        with env.sessions.transaction() as session:
            assert session.scalar(select(func.count()).select_from(AttachmentRecord)) == before + 1
            assert session.get(FileOperationReceiptRecord, result.job_id)
        assert (
            env.attachments.storage_root
            / env.attachments.get_metadata(result.attachment_id).relative_path
        ).read_bytes() == b"edit before crash"


def test_recovery_partition_restore_is_not_replayed_and_cache_generation_rotates(tmp_path):
    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    with lifecycle(paths.data_dir, attachment_root=paths.attachment_dir) as (env, facade):
        assert env.attachments.storage_root == paths.attachment_dir
        item = original(env, facade)
        view = facade.prepare_open("attachment", item.id)
        cache = facade.cache.by_path(view)
        unknown = paths.attachment_dir / "unregistered.txt"
        unknown.write_bytes(b"uncertain evidence")
        manager = BackupManager(paths, coordinator=env.attachments.coordinator)
        backup = manager.create_backup()
        manifest = manager.verify_backup(backup.path)
        assert not any(file.path.startswith("cache/") for file in manifest.attachments)
    manager.stage_restore(backup.path)
    manager.apply_pending_restore()
    assert not unknown.exists()
    assert (
        next((paths.root / "recovery").rglob("unregistered.txt")).read_bytes()
        == b"uncertain evidence"
    )
    assert view.exists()
    marker = json.loads(
        (paths.attachment_dir / "cache/attachment-views/generation.json").read_text()
    )
    assert marker["generation"] != cache.generation


@pytest.mark.parametrize(
    "stage", ["discard_approved", "discard_file_deleted", "discard_metadata_removed"]
)
def test_discard_actual_process_death_only_finishes_deleted_metadata(tmp_path, stage):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        path = facade.prepare_open("attachment", item.id)
        copy = facade.cache.by_path(path)
        facade.cache.confirm_closed(copy, copy.use_token)
    script = """
import os,sys
from pathlib import Path
from tests.integration.test_document_lifecycle import lifecycle
with lifecycle(Path(sys.argv[1])) as (env,facade):
    facade.cache._checkpoint = lambda point: os._exit(77) if point == sys.argv[2] else None
    facade.cache.discard(facade.cache.page()[0])
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), stage],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 77, child.stderr
    with lifecycle(tmp_path) as (env, facade):
        while not facade.cache.repair_batch(20):
            pass
        if stage == "discard_approved":
            assert path.read_bytes() == b"initial"  # never replay even an approved byte deletion
            facade.cache.discard(facade.cache.page()[0])
        assert not path.exists()
        assert facade.cache.page() == ()
        assert not tuple(facade.cache.base.glob("discard-*.json"))
        assert (env.attachments.storage_root / item.relative_path).read_bytes() == b"initial"


def test_later_edit_after_preservation_creates_new_work_not_old_receipt(tmp_path, monkeypatch):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        path = facade.prepare_open("attachment", item.id)
        path.write_bytes(b"first saved edit")
        copy = facade.cache.by_path(path)
        copy = facade.cache.confirm_closed(copy, copy.use_token)
        discard = facade.cache.discard
        monkeypatch.setattr(
            facade.cache,
            "discard",
            lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("locked")),
        )
        first = facade.cache.preserve(copy, "first.txt", facade.documents, lambda: False)
        assert first.warning and path.exists()
        path.write_bytes(b"later saved edit")
        copy = facade.cache.confirm_closed(facade.cache.by_path(path), copy.use_token)
        monkeypatch.setattr(facade.cache, "discard", discard)
        second = facade.cache.preserve(copy, "second.txt", facade.documents, lambda: False)
        assert second.job_id != first.job_id and second.attachment_id != first.attachment_id
        assert (
            env.attachments.storage_root
            / env.attachments.get_metadata(first.attachment_id).relative_path
        ).read_bytes() == b"first saved edit"
        assert (
            env.attachments.storage_root
            / env.attachments.get_metadata(second.attachment_id).relative_path
        ).read_bytes() == b"later saved edit"


def test_bad_front_cache_candidates_do_not_starve_next_batch(tmp_path, monkeypatch):
    with lifecycle(tmp_path) as (_env, facade):
        cache = facade.cache
        cache._ready = True
        with cache._connection() as db:
            db.executemany(
                "INSERT INTO copies VALUES(?,?,?,?,?,?,?)",
                [
                    (cache.generation, f"{n:032x}", "a" * 32, "normal", 0, 0, 10)
                    for n in range(50000)
                ],
            )
        checked = []

        def unavailable(generation, identity):
            checked.append(identity)
            raise PermissionError("locked or missing")

        monkeypatch.setattr(cache, "get", unavailable)
        cache.maintenance()
        first = set(checked)
        assert len(first) == 20
        cache.maintenance()
        assert len(checked) == 40 and not first.intersection(checked[20:])


def test_open_cache_limit_preserves_two_modified_copies_and_no_auto_delete(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        paths = []
        for n in range(2):
            path = facade.prepare_open("attachment", item.id)
            path.write_bytes(f"edit {n}".encode())
            paths.append(path)
        with pytest.raises(RuntimeError, match="보호/보기 사본"):
            facade.prepare_open("attachment", item.id)
        assert all(path.exists() for path in paths)


def test_locked_cache_cannot_be_discarded_even_when_closed_confirmed(tmp_path):
    from officeflow.infrastructure.attachments.read_lease import protected_read

    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        path = facade.prepare_open("attachment", item.id)
        copy = facade.cache.by_path(path)
        copy = facade.cache.confirm_closed(copy, copy.use_token)
        with protected_read(path), pytest.raises(OSError):
            facade.cache.discard(copy)
        assert path.exists()


def test_work_default_names_and_current_source_follow_rename(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        entry = facade.catalog.get("attachment", item.id)
        name = facade.catalog.work_suggestion(entry.task_id, entry.name)
        result = facade.create(entry, DocumentOperation.WORK, filename=name)
        assert facade.catalog.work_suggestion(entry.task_id, entry.name) != name
        renamed = env.attachments.rename_attachment(
            item.id, "새 이름.txt", expected_revision=item.metadata_revision
        )
        work = facade.catalog.get("attachment", result.attachment_id)
        assert work.current_source_name == renamed.name
        assert work.name == name


def test_old_generation_modified_cache_can_be_saved_externally(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        path = facade.prepare_open("attachment", item.id)
        path.write_bytes(b"valuable old generation edit")
        copy = facade.cache.by_path(path)
        facade.cache.generation = "b" * 32
        exports = AttachmentExportService(
            env.attachments, ExternalExportStorage(tmp_path / "separate-export-state")
        )
        target = tmp_path / "salvaged.txt"
        result = exports.export((FileReference.cache(copy.relative_path),), target, single=True)
        assert result.items[0].status is ExportStatus.SAVED
        assert target.read_bytes() == path.read_bytes()
        assert path.exists()


@pytest.mark.parametrize("point", ["before_marker", "after_marker"])
def test_restore_actual_death_after_cache_move_rotates_generation_idempotently(tmp_path, point):
    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    with lifecycle(paths.data_dir, attachment_root=paths.attachment_dir) as (env, facade):
        item = original(env, facade)
        view = facade.prepare_open("attachment", item.id)
        previous = facade.cache.generation
        manager = BackupManager(paths, coordinator=env.attachments.coordinator)
        backup = manager.create_backup()
    manager.stage_restore(backup.path)
    script = """
import os,sys
from pathlib import Path
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.backup import BackupManager
replace = os.replace
def interrupted(source, target):
    if Path(target).name == 'generation.json':
        if sys.argv[2] == 'before_marker': os._exit(79)
        replace(source,target)
        os._exit(79)
    return replace(source,target)
os.replace = interrupted
BackupManager(AppPaths(Path(sys.argv[1]))).apply_pending_restore()
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(paths.root), point],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 79, child.stderr
    manager.apply_pending_restore()
    marker = paths.attachment_dir / "cache/attachment-views/generation.json"
    current = json.loads(marker.read_text())["generation"]
    assert current != previous and view.read_bytes() == b"initial"
    manager.apply_pending_restore()
    assert json.loads(marker.read_text())["generation"] == current


def test_two_cache_preservation_clicks_cannot_create_duplicate_work(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        path = facade.prepare_open("attachment", item.id)
        path.write_bytes(b"saved edit")
        copy = facade.cache.by_path(path)
        copy = facade.cache.confirm_closed(copy, copy.use_token)

        def preserve():
            try:
                return facade.cache.preserve(copy, "작업본.txt", facade.documents, lambda: False)
            except FileNotFoundError:
                return None  # the winning request already preserved and cleaned this copy

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = tuple(workers.map(lambda _n: preserve(), range(2)))
        assert sum(result is not None for result in results) == 1
        works = [
            row
            for row in env.attachments.attachments_for_task(item.task_id)
            if row.role.value == "work"
        ]
        assert len(works) == 1
