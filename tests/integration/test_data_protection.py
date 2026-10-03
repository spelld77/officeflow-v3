"""Failure-injection regressions for consistent backup and crash recovery."""

import json
import os
import shutil
import zipfile
from pathlib import Path
from threading import Event, Thread

import pytest

import officeflow.infrastructure.backup as backup_module
from officeflow.application.tasks import TaskDraft
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.backup import BackupError, BackupManager
from officeflow.presentation.data_dialog import DataManagementDialog
from tests.integration.test_attachment_search import search_environment
from tests.integration.test_backup import _database, _values


@pytest.fixture
def attachment_env(tmp_path):
    paths = AppPaths(tmp_path / "isolated")
    paths.ensure_directories()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        task = env.tasks.create(TaskDraft(title="보호할 업무"))
        source = tmp_path / "original.txt"
        source.write_bytes(b"preserved original bytes")
        attached = env.attachments.attach(task.id, source)
        yield paths, env, attached


def archive_bytes(path: Path, relative: str) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read(f"attachments/{relative}")


def test_file_disappearing_after_database_snapshot_is_not_a_successful_backup(
    attachment_env, monkeypatch
):
    paths, env, attached = attachment_env
    manager = BackupManager(paths)
    original = manager._snapshot_database

    def snapshot_then_delete(destination, canceled):
        original(destination, canceled)
        env.attachments.delete_file(attached.id)

    monkeypatch.setattr(manager, "_snapshot_database", snapshot_then_delete)
    with pytest.raises(BackupError, match="첨부파일이 변경되거나 사라"):
        manager.create_backup()
    assert not list(paths.backup_dir.iterdir())
    assert not list(paths.root.glob(".backup-work-*"))


@pytest.mark.parametrize("mutation", ["delete", "unlink", "add"])
def test_mutation_after_capture_preserves_original_snapshot_and_does_not_block_writer(
    attachment_env, tmp_path, monkeypatch, mutation
):
    paths, env, attached = attachment_env
    manager = BackupManager(paths)
    original = manager._stage_attachment_sources
    errors = []

    def mutate():
        try:
            if mutation == "delete":
                env.attachments.delete_file(attached.id)
            elif mutation == "unlink":
                env.attachments.unlink(attached.id)
            else:
                source = tmp_path / "new.txt"
                source.write_bytes(b"new attachment")
                env.attachments.attach(attached.task_id, source)
        except Exception as error:
            errors.append(error)

    def stage(sources, destination, canceled):
        thread = Thread(target=mutate)
        thread.start()
        thread.join(timeout=3)
        assert not thread.is_alive(), "Compression/staging must not hold the mutation gate"
        return original(sources, destination, canceled)

    monkeypatch.setattr(manager, "_stage_attachment_sources", stage)
    backup = manager.create_backup()
    assert errors == []
    manifest = manager.verify_backup(backup.path)
    assert manifest.missing_attachments == ()
    assert {item.path for item in manifest.attachments} == {attached.relative_path}
    assert archive_bytes(backup.path, attached.relative_path) == b"preserved original bytes"


def test_delete_waits_for_capture_but_not_compression(attachment_env, monkeypatch):
    paths, env, attached = attachment_env
    manager = BackupManager(paths)
    original = manager._snapshot_database
    started, finished = Event(), Event()
    errors = []

    def delete():
        started.set()
        try:
            env.attachments.delete_file(attached.id)
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    thread = Thread(target=delete)

    def snapshot(destination, canceled):
        original(destination, canceled)
        thread.start()
        assert started.wait(1)
        assert not finished.wait(0.03)

    monkeypatch.setattr(manager, "_snapshot_database", snapshot)
    try:
        backup = manager.create_backup()
    finally:
        thread.join(timeout=3)
    assert finished.is_set() and errors == []
    assert archive_bytes(backup.path, attached.relative_path) == b"preserved original bytes"


def test_external_content_change_after_capture_rejects_backup(attachment_env, monkeypatch):
    paths, _env, attached = attachment_env
    manager = BackupManager(paths)
    original = manager._stage_attachment_sources

    def stage(sources, destination, canceled):
        (paths.attachment_dir / attached.relative_path).write_bytes(b"external overwrite")
        return original(sources, destination, canceled)

    monkeypatch.setattr(manager, "_stage_attachment_sources", stage)
    with pytest.raises(BackupError, match="내용이 변경"):
        manager.create_backup()
    assert not list(paths.backup_dir.iterdir())


def test_external_change_during_copy_rejects_backup(attachment_env, monkeypatch):
    paths, _env, attached = attachment_env
    original = backup_module._copy_stream

    def copy(source, target, canceled):
        original(source, target, canceled)
        if "/captures/" in str(source.name).replace("\\", "/"):
            (paths.attachment_dir / attached.relative_path).write_bytes(b"changed during copy")

    monkeypatch.setattr(backup_module, "_copy_stream", copy)
    with pytest.raises(BackupError, match="사본 확보 중 파일이 변경"):
        BackupManager(paths).create_backup()
    assert not list(paths.backup_dir.iterdir())


def test_backup_copies_sources_on_filesystems_without_hardlinks(attachment_env, monkeypatch):
    paths, _env, attached = attachment_env

    def unsupported(*_args):
        raise OSError("hard links unsupported")

    monkeypatch.setattr(backup_module.os, "link", unsupported)
    backup = BackupManager(paths).create_backup()
    assert archive_bytes(backup.path, attached.relative_path) == b"preserved original bytes"


@pytest.mark.parametrize("failure", ["copy", "cancel"])
def test_capture_failure_leaves_no_partial_backup(attachment_env, monkeypatch, failure):
    paths, env, attached = attachment_env

    def failed(*_args):
        if failure == "cancel":
            raise BackupError("백업을 취소했습니다.")
        raise PermissionError("synthetic copy permission failure")

    monkeypatch.setattr(backup_module, "_stable_copy", failed)
    with pytest.raises(BackupError):
        BackupManager(paths).create_backup()
    assert env.attachments.path_for_open(attached.id).read_bytes() == b"preserved original bytes"
    assert not list(paths.backup_dir.iterdir())
    assert not list(paths.root.glob(".backup-work-*"))


def test_preexisting_missing_file_is_explicit_in_backup_and_restore_message(attachment_env):
    paths, _env, attached = attachment_env
    (paths.attachment_dir / attached.relative_path).unlink()
    manager = BackupManager(paths)
    backup = manager.create_backup()
    assert backup.missing_attachments == (attached.relative_path,)
    assert "기존 누락 첨부 1개" in DataManagementDialog._backup_message(backup)
    manifest = manager.verify_backup(backup.path)
    assert manifest.format_version == 2
    assert manifest.missing_attachments == backup.missing_attachments
    assert manager.stage_restore(backup.path).missing_attachments == backup.missing_attachments


@pytest.mark.parametrize("version", [1, 2])
def test_old_missing_file_is_reported_but_new_undeclared_missing_is_rejected(
    attachment_env, version
):
    paths, _env, _attached = attachment_env
    manager = BackupManager(paths)
    backup = manager.create_backup()
    changed = paths.backup_dir / f"changed-{version}.ofbackup"
    with zipfile.ZipFile(backup.path) as source, zipfile.ZipFile(changed, "w") as target:
        for name in source.namelist():
            if name.startswith("attachments/"):
                continue
            data = source.read(name)
            if name == "manifest.json":
                raw = json.loads(data)
                raw["format_version"] = version
                raw["attachments"] = []
                raw.pop("missing_attachments")
                data = json.dumps(raw).encode()
            target.writestr(name, data)
    if version == 1:
        manifest = manager.verify_backup(changed)
        assert len(manifest.missing_attachments) == 1
    else:
        with pytest.raises(BackupError, match="DB의 첨부 연결"):
            manager.verify_backup(changed)


def restore_environment(tmp_path):
    paths = AppPaths(tmp_path / "restore")
    paths.ensure_directories()
    _database(paths.database_file, ("backup",))
    source = paths.attachment_dir / "original.txt"
    source.write_bytes(b"backup content")
    paths.settings_file.write_text('{"value":"backup"}', encoding="utf-8")
    manager = BackupManager(paths)
    backup = manager.create_backup()
    _database(paths.database_file, ("before restore",))
    source.write_bytes(b"current content")
    paths.settings_file.write_text('{"value":"current"}', encoding="utf-8")
    manager.stage_restore(backup.path)
    return paths, manager, source


@pytest.mark.parametrize("step", ["pending", "rollback", "staging"])
def test_post_commit_cleanup_failure_never_rolls_back_restored_data(tmp_path, monkeypatch, step):
    paths, manager, source = restore_environment(tmp_path)
    original_rmtree, original_unlink = shutil.rmtree, Path.unlink
    injected = False

    def cleanup(path, *args, **kwargs):
        nonlocal injected
        target = Path(path)
        if not injected and step == "rollback" and target.name.startswith(".restore-rollback-"):
            injected = True
            (target / "attachments" / "original.txt").unlink()
            raise PermissionError("partially cleaned old files")
        if not injected and step == "staging" and target.name.startswith(".restore-stage-"):
            injected = True
            raise PermissionError("stage cleanup denied")
        return original_rmtree(path, *args, **kwargs)

    def unlink(path, *args, **kwargs):
        nonlocal injected
        if not injected and step == "pending" and path == paths.pending_restore_file:
            injected = True
            raise PermissionError("pending cleanup denied")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(backup_module.shutil, "rmtree", cleanup)
        patch.setattr(Path, "unlink", unlink)
        protection = manager.apply_pending_restore()
    assert injected and protection is not None and manager.restore_warnings
    assert _values(paths.database_file) == ("backup",)
    assert source.read_bytes() == b"backup content"
    assert paths.settings_file.read_text() == '{"value":"backup"}'
    assert paths.root.joinpath(".restore-state.json").is_file()
    assert (
        json.loads(paths.root.joinpath(".restore-state.json").read_text())["phase"] == "committed"
    )
    BackupManager(paths).apply_pending_restore()
    assert _values(paths.database_file) == ("backup",)
    assert not paths.pending_restore_file.exists()
    assert not list(paths.root.glob(".restore-*"))
    assert manager.verify_backup(protection.path).table_counts["sample"] == 1


@pytest.mark.parametrize(
    "move",
    ["old-db", "old-attachments", "old-settings", "new-db", "new-attachments", "new-settings"],
)
@pytest.mark.parametrize("abrupt", [False, True])
def test_restore_interrupted_at_each_move_recovers_original_or_finishes_on_restart(
    tmp_path, monkeypatch, move, abrupt
):
    paths, manager, source = restore_environment(tmp_path)
    original_replace = os.replace
    injected = False

    def replace_file(origin, target):
        nonlocal injected
        origin, target = Path(origin), Path(target)
        matches = {
            "old-db": origin == paths.database_file and target.parent.name == "data",
            "old-attachments": origin == paths.attachment_dir,
            "old-settings": origin == paths.settings_file,
            "new-db": target == paths.database_file and ".restore-stage-" in str(origin),
            "new-attachments": target == paths.attachment_dir and ".restore-stage-" in str(origin),
            "new-settings": target == paths.settings_file and ".restore-stage-" in str(origin),
        }
        result = original_replace(origin, target)
        if not injected and matches[move]:
            injected = True
            if abrupt:
                raise SystemExit("simulated power loss")
            raise PermissionError("simulated move failure")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(backup_module.os, "replace", replace_file)
        with pytest.raises(SystemExit if abrupt else BackupError):
            manager.apply_pending_restore()
    assert injected
    if not abrupt:
        assert _values(paths.database_file) == ("before restore",)
        assert source.read_bytes() == b"current content"
        assert paths.settings_file.read_text() == '{"value":"current"}'
    BackupManager(paths).apply_pending_restore()
    assert _values(paths.database_file) == ("backup",)
    assert source.read_bytes() == b"backup content"
    assert paths.settings_file.read_text() == '{"value":"backup"}'
    assert not list(paths.root.glob(".restore-*"))


def test_restore_validation_failure_returns_original_data_and_keeps_pending(tmp_path, monkeypatch):
    paths, manager, source = restore_environment(tmp_path)
    original = manager._swap_restored_data

    def fail(staging, rollback, manifest):
        original(staging, rollback, manifest)
        raise BackupError("synthetic validation failure")

    monkeypatch.setattr(manager, "_swap_restored_data", fail)
    with pytest.raises(BackupError, match="validation"):
        manager.apply_pending_restore()
    assert paths.pending_restore_file.exists()
    assert _values(paths.database_file) == ("before restore",)
    assert source.read_bytes() == b"current content"
    assert paths.settings_file.read_text() == '{"value":"current"}'


def test_failure_to_rollback_keeps_transaction_and_is_recoverable(tmp_path, monkeypatch):
    paths, manager, source = restore_environment(tmp_path)
    original = manager._swap_restored_data

    def fail(staging, rollback, manifest):
        original(staging, rollback, manifest)
        raise BackupError("synthetic validation failure")

    def fail_rollback(*_args, **_kwargs):
        raise PermissionError("rollback denied")

    with monkeypatch.context() as patch:
        patch.setattr(manager, "_swap_restored_data", fail)
        patch.setattr(manager, "_rollback_restore", fail_rollback)
        with pytest.raises(BackupError, match="현재 DB를 열지"):
            manager.apply_pending_restore()
        assert manager.restore_is_incomplete
    assert list(paths.root.glob(".restore-rollback-*"))
    BackupManager(paths).apply_pending_restore()
    assert source.read_bytes() == b"backup content"
    assert not manager.restore_is_incomplete


def test_invalid_restore_journal_is_rejected_without_touching_outside_data(tmp_path):
    paths, manager, source = restore_environment(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"do not touch")
    paths.root.joinpath(".restore-state.json").write_text(
        json.dumps(
            {
                "phase": "committed",
                "rollback": "../outside",
                "staging": "../outside",
            }
        )
    )
    with pytest.raises(BackupError, match="보호"):
        manager.apply_pending_restore()
    assert manager.restore_is_incomplete
    assert outside.read_bytes() == b"do not touch"
    assert source.read_bytes() == b"current content"


@pytest.mark.parametrize("committed", [False, True])
def test_attachment_delete_crash_is_resolved_from_database_state(
    attachment_env, monkeypatch, committed
):
    paths, env, attached = attachment_env
    storage = env.attachments._storage

    def abrupt(*_args):
        raise SystemExit("simulated termination")

    with monkeypatch.context() as patch:
        patch.setattr(
            storage if committed else env.repository,
            "purge" if committed else "delete_attachment",
            abrupt,
        )
        with pytest.raises(SystemExit):
            env.attachments.delete_file(attached.id)
    manager = BackupManager(paths)
    usage = manager.inspect_data_usage()
    assert usage.quarantine_file_count > 0 and usage.quarantine_bytes > 0
    assert usage.orphan_attachment_count == 0
    assert manager.recover_attachment_deletions() == ()
    assert manager.recover_attachment_deletions() == ()
    if committed:
        assert env.repository.get_attachment(attached.id) is None
        assert not (paths.attachment_dir / attached.relative_path).exists()
    else:
        assert (
            env.attachments.path_for_open(attached.id).read_bytes() == b"preserved original bytes"
        )
        assert env.repository.get_attachment(attached.id) is not None
    assert not list((paths.attachment_dir / ".trash").rglob("*"))


def test_crash_after_deletion_journal_before_move_keeps_original(attachment_env, monkeypatch):
    paths, env, attached = attachment_env
    original_replace = os.replace

    def replace_file(source, destination):
        if Path(source) == paths.attachment_dir / attached.relative_path:
            raise SystemExit("before move")
        return original_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace_file)
        with pytest.raises(SystemExit):
            env.attachments.delete_file(attached.id)
    assert BackupManager(paths).recover_attachment_deletions() == ()
    assert env.attachments.path_for_open(attached.id).read_bytes() == b"preserved original bytes"


def test_old_quarantine_without_journal_is_restored_by_unique_stored_name(attachment_env):
    paths, env, attached = attachment_env
    target = paths.attachment_dir / ".trash" / "legacy" / attached.stored_name
    target.parent.mkdir(parents=True)
    os.replace(paths.attachment_dir / attached.relative_path, target)
    assert BackupManager(paths).recover_attachment_deletions() == ()
    assert env.attachments.path_for_open(attached.id).read_bytes() == b"preserved original bytes"


@pytest.mark.parametrize("kind", ["unknown", "invalid-journal", "conflicting-original", "flat", "hidden"])
def test_unresolved_quarantine_is_preserved_in_usage_and_backup(attachment_env, kind):
    paths, _env, attached = attachment_env
    target = paths.attachment_dir / ".trash" / "unknown" / attached.stored_name
    if kind == "flat":
        target = paths.attachment_dir / ".trash" / "unidentified.bin"
    elif kind == "hidden":
        target = paths.attachment_dir / ".trash" / "unknown" / ".unidentified.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"valuable unknown data")
    if kind == "invalid-journal":
        target.with_name("deletion.json").write_text(
            '{"original":"../escape","quarantined":"../escape"}'
        )
    elif kind == "conflicting-original":
        target.with_name("deletion.json").write_text(
            json.dumps(
                {
                    "original": attached.relative_path,
                    "quarantined": target.relative_to(paths.attachment_dir).as_posix(),
                }
            )
        )
    manager = BackupManager(paths)
    assert manager.recover_attachment_deletions()
    usage = manager.inspect_data_usage()
    assert usage.quarantine_bytes >= len(b"valuable unknown data")
    assert (
        usage.total_bytes
        >= usage.database_bytes + usage.stored_attachment_bytes + usage.quarantine_bytes
    )
    relative = target.relative_to(paths.attachment_dir).as_posix()
    with pytest.raises(BackupError, match="격리"):
        manager.delete_orphan_attachment_file(relative)
    backup = manager.create_backup()
    assert archive_bytes(backup.path, relative) == b"valuable unknown data"
    assert target.read_bytes() == b"valuable unknown data"


@pytest.mark.parametrize("boundary", ["committed", "rollback-db-returned"])
def test_restart_uses_durable_phase_after_commit_or_during_rollback(
    tmp_path, monkeypatch, boundary
):
    paths, manager, source = restore_environment(tmp_path)
    original_state = manager._write_restore_state
    original_swap = manager._swap_restored_data
    original_replace = os.replace

    def state_saved(state):
        original_state(state)
        if state["phase"] == "committed" and boundary == "committed":
            raise SystemExit("after durable commit")

    def fail_swap(*args):
        original_swap(*args)
        if boundary == "rollback-db-returned":
            raise PermissionError("force rollback")

    def replace_file(origin, target):
        result = original_replace(origin, target)
        if (
            boundary == "rollback-db-returned"
            and Path(target) == paths.database_file
            and ".restore-rollback-" in str(origin)
        ):
            raise SystemExit("during rollback after DB was returned")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(manager, "_write_restore_state", state_saved)
        patch.setattr(manager, "_swap_restored_data", fail_swap)
        patch.setattr(os, "replace", replace_file)
        with pytest.raises(SystemExit):
            manager.apply_pending_restore()
    BackupManager(paths).apply_pending_restore()
    assert _values(paths.database_file) == ("backup",)
    assert source.read_bytes() == b"backup content"
    assert paths.settings_file.read_text() == '{"value":"backup"}'
    assert not list(paths.root.glob(".restore-*"))


def test_restore_without_settings_keeps_current_settings(tmp_path):
    paths, manager, _source = restore_environment(tmp_path)
    paths.pending_restore_file.unlink()
    paths.settings_file.unlink()
    without_settings = manager.create_backup()
    paths.settings_file.write_text('{"keep":"this"}')
    manager.stage_restore(without_settings.path)
    manager.apply_pending_restore()
    assert paths.settings_file.read_text() == '{"keep":"this"}'


def test_real_database_and_attachments_round_trip_after_delete(attachment_env):
    paths, env, attached = attachment_env
    manager = BackupManager(paths)
    backup = manager.create_backup()
    env.attachments.delete_file(attached.id)
    env.engine.dispose()
    manager.stage_restore(backup.path)
    manager.apply_pending_restore()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as restored:
        assert restored.repository.get_attachment(attached.id) is not None
        assert (
            restored.attachments.path_for_open(attached.id).read_bytes()
            == b"preserved original bytes"
        )
        assert manager.inspect_data_usage().missing_attachment_count == 0


def test_crash_after_file_purge_before_journal_cleanup_is_resumable(attachment_env, monkeypatch):
    paths, env, attached = attachment_env
    original_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path.name == "deletion.json":
            raise SystemExit("after payload purge")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", unlink)
        with pytest.raises(SystemExit):
            env.attachments.delete_file(attached.id)
    assert env.repository.get_attachment(attached.id) is None
    assert BackupManager(paths).recover_attachment_deletions() == ()
    assert not list((paths.attachment_dir / ".trash").rglob("*"))


def test_startup_recovers_uncommitted_attachment_delete(attachment_env, monkeypatch, qapp, qtbot):
    from officeflow.main import build_application

    paths, env, attached = attachment_env

    def abrupt(*_args):
        raise SystemExit("before DB commit")

    with monkeypatch.context() as patch:
        patch.setattr(env.repository, "delete_attachment", abrupt)
        with pytest.raises(SystemExit):
            env.attachments.delete_file(attached.id)
    monkeypatch.setenv("OFFICEFLOW_DATA_DIR", str(paths.root))
    _, window = build_application(application=qapp)
    qtbot.addWidget(window)
    try:
        assert (
            env.attachments.path_for_open(attached.id).read_bytes() == b"preserved original bytes"
        )
    finally:
        window.shutdown()
        window.close()


def test_startup_refuses_to_open_database_with_invalid_restore_state(tmp_path, monkeypatch, qapp):
    from officeflow.main import build_application

    paths, _manager, source = restore_environment(tmp_path)
    paths.root.joinpath(".restore-state.json").write_text("invalid JSON")
    monkeypatch.setenv("OFFICEFLOW_DATA_DIR", str(paths.root))
    with pytest.raises(BackupError):
        build_application(application=qapp)
    assert source.read_bytes() == b"current content"


def test_failed_deletion_recovery_keeps_file_when_database_cannot_be_read(attachment_env, monkeypatch):
    paths, env, attached = attachment_env

    def abrupt(*_args):
        raise SystemExit("before DB commit")

    with monkeypatch.context() as patch:
        patch.setattr(env.repository, "delete_attachment", abrupt)
        with pytest.raises(SystemExit):
            env.attachments.delete_file(attached.id)
    manager = BackupManager(paths)

    def failed():
        raise BackupError("DB state unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(manager, "_tracked_attachment_paths", failed)
        with pytest.raises(BackupError):
            manager.recover_attachment_deletions()
    assert list((paths.attachment_dir / ".trash").glob("*/deletion.json"))
    assert manager.recover_attachment_deletions() == ()
    assert env.attachments.path_for_open(attached.id).read_bytes() == b"preserved original bytes"
