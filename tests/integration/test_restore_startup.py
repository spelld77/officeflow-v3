import sqlite3
from contextlib import closing

import pytest

from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.backup import (
    BackupCanceledError,
    BackupError,
    BackupManager,
    _database_counts,
)
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.main import build_application, prepare_pending_restore


def _manager(tmp_path):
    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    return paths, BackupManager(paths)


def test_failed_restore_exit_keeps_reservation_and_current_database(tmp_path):
    paths, manager = _manager(tmp_path)
    before = paths.database_file.read_bytes()
    paths.pending_restore_file.write_bytes(b"damaged backup")
    seen = []
    with pytest.raises(BackupError, match="시작을 중단"):
        prepare_pending_restore(manager, on_failure=lambda error: seen.append(str(error)) or "exit")
    assert seen and paths.pending_restore_file.read_bytes() == b"damaged backup"
    assert paths.database_file.read_bytes() == before


def test_cancel_failed_restore_disarms_it_and_retains_archive(tmp_path):
    paths, manager = _manager(tmp_path)
    paths.pending_restore_file.write_bytes(b"damaged backup")
    prepare_pending_restore(manager, on_failure=lambda _error: "cancel")
    assert not paths.pending_restore_file.exists()
    retained = list(paths.backup_dir.glob("canceled-restore-*.ofbackup"))
    assert len(retained) == 1 and retained[0].read_bytes() == b"damaged backup"
    assert manager.restore_warnings
    prepare_pending_restore(
        manager, on_failure=lambda _error: pytest.fail("reservation was not canceled")
    )


def test_retry_after_transient_restore_failure_succeeds(tmp_path, monkeypatch):
    _paths, manager = _manager(tmp_path)
    calls = []

    def apply():
        calls.append(True)
        if len(calls) == 1:
            raise BackupError("일시적 파일 잠금")

    monkeypatch.setattr(manager, "apply_pending_restore", apply)
    prepare_pending_restore(manager, on_failure=lambda _error: "retry")
    assert len(calls) == 2


def test_incomplete_restore_cannot_be_bypassed_by_cancel(tmp_path):
    paths, manager = _manager(tmp_path)
    paths.root.joinpath(".restore-state.json").write_text("invalid state")
    with pytest.raises(BackupError):
        prepare_pending_restore(
            manager, on_failure=lambda _error: pytest.fail("must not offer unsafe continuation")
        )
    with pytest.raises(BackupError):
        manager.cancel_pending_restore()


def test_unknown_database_revision_is_rejected_before_reservation(tmp_path):
    paths, manager = _manager(tmp_path)
    with closing(sqlite3.connect(paths.database_file)) as connection:
        connection.execute("UPDATE alembic_version SET version_num='9999_future'")
        connection.commit()
    archive = manager.create_backup()
    with pytest.raises(BackupError, match="지원하지 않는 DB 버전"):
        manager.stage_restore(archive.path)
    assert not paths.pending_restore_file.exists()


def test_non_officeflow_database_is_not_restored(tmp_path):
    paths = AppPaths(tmp_path / "generic")
    paths.ensure_directories()
    with closing(sqlite3.connect(paths.database_file)) as connection:
        connection.execute("CREATE TABLE unrelated(id INTEGER)")
    manager = BackupManager(paths)
    archive = manager.create_backup()
    with pytest.raises(BackupError, match="OfficeFlow v3"):
        manager.stage_restore(archive.path)


def test_database_validation_can_be_cancelled_without_modifying_db(tmp_path):
    paths, _manager_instance = _manager(tmp_path)
    before = paths.database_file.read_bytes()
    with pytest.raises(BackupCanceledError):
        _database_counts(paths.database_file, lambda: True)
    assert paths.database_file.read_bytes() == before


def test_database_validation_can_cancel_during_sqlite_scan(tmp_path):
    paths, _manager_instance = _manager(tmp_path)
    with closing(sqlite3.connect(paths.database_file)) as connection:
        connection.execute("CREATE TABLE payload(value TEXT)")
        connection.executemany(
            "INSERT INTO payload VALUES (?)", (("x" * 1000,) for _ in range(5000))
        )
        connection.commit()
    before = paths.database_file.read_bytes()
    calls = 0

    def canceled():
        nonlocal calls
        calls += 1
        return calls >= 3

    with pytest.raises(BackupCanceledError):
        _database_counts(paths.database_file, canceled)
    assert calls >= 3 and paths.database_file.read_bytes() == before


def test_application_can_cancel_failed_restore_and_open_current_data(
    tmp_path, monkeypatch, qapp, qtbot
):
    paths, _manager_instance = _manager(tmp_path)
    paths.pending_restore_file.write_bytes(b"broken reservation")
    monkeypatch.setenv("OFFICEFLOW_DATA_DIR", str(paths.root))
    _, window = build_application(application=qapp, restore_failure_handler=lambda _error: "cancel")
    qtbot.addWidget(window)
    try:
        assert not paths.pending_restore_file.exists()
        assert len(list(paths.backup_dir.glob("canceled-restore-*.ofbackup"))) == 1
        assert "복원 예약을 취소" in window.statusBar().currentMessage()
    finally:
        window.shutdown()
