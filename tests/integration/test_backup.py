from __future__ import annotations

import os
import sqlite3
import zipfile
from contextlib import closing

import pytest

from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.backup import BackupError, BackupManager


def _database(path, values: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("DELETE FROM sample")
        connection.executemany("INSERT INTO sample(value) VALUES (?)", ((value,) for value in values))
        connection.commit()


def _values(path) -> tuple[str, ...]:
    with closing(sqlite3.connect(path)) as connection:
        return tuple(row[0] for row in connection.execute("SELECT value FROM sample ORDER BY id"))


def test_online_backup_verifies_and_restores_database_attachments_and_settings(tmp_path) -> None:
    paths = AppPaths(tmp_path / "officeflow")
    paths.ensure_directories()
    _database(paths.database_file, ("before",))
    attachment = paths.attachment_dir / "ab" / "original.bin"
    attachment.parent.mkdir()
    attachment.write_bytes(b"original attachment")
    paths.settings_file.write_text('{"theme": "light"}', encoding="utf-8")
    manager = BackupManager(paths)

    created = manager.create_backup(reason="manual")
    manifest = manager.verify_backup(created.path)
    assert manifest.table_counts["sample"] == 1
    assert len(manifest.attachments) == 1

    _database(paths.database_file, ("changed", "second"))
    attachment.write_bytes(b"changed")
    paths.settings_file.write_text('{"theme": "dark"}', encoding="utf-8")
    manager.stage_restore(created.path)
    pre_restore = manager.apply_pending_restore()

    assert pre_restore is not None
    assert pre_restore.reason == "pre-restore"
    assert _values(paths.database_file) == ("before",)
    assert attachment.read_bytes() == b"original attachment"
    assert paths.settings_file.read_text(encoding="utf-8") == '{"theme": "light"}'
    assert not paths.pending_restore_file.exists()
    assert manager.verify_backup(pre_restore.path).table_counts["sample"] == 2


def test_backup_verification_rejects_tampered_attachment(tmp_path) -> None:
    paths = AppPaths(tmp_path / "officeflow")
    paths.ensure_directories()
    _database(paths.database_file, ("one",))
    attachment = paths.attachment_dir / "ab" / "file.bin"
    attachment.parent.mkdir()
    attachment.write_bytes(b"safe")
    manager = BackupManager(paths)
    created = manager.create_backup()
    tampered = paths.backup_dir / "tampered.ofbackup"

    with zipfile.ZipFile(created.path, "r") as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename.startswith("attachments/"):
                data = b"tampered"
            target.writestr(info, data)

    with pytest.raises(BackupError, match=r"크기|체크섬"):
        manager.verify_backup(tampered)


def test_automatic_backup_retention_removes_only_old_automatic_files(tmp_path) -> None:
    paths = AppPaths(tmp_path / "officeflow")
    paths.ensure_directories()
    _database(paths.database_file, ("one",))
    manager = BackupManager(paths)
    automatic = [manager.create_backup(reason="automatic").path for _ in range(3)]
    manual = manager.create_backup(reason="manual").path
    for index, path in enumerate(automatic):
        os.utime(path, (100 + index, 100 + index))

    removed = manager.prune_automatic_backups(2)

    assert removed == (automatic[0],)
    assert manual.exists()
    assert not automatic[0].exists()
    assert automatic[1].exists() and automatic[2].exists()
