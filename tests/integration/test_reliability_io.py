from __future__ import annotations

from contextlib import contextmanager

from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.data_inspection import (
    ManagedInspectionReader,
    SqliteInspectionRepository,
)
from tests.integration.test_attachment_documents import documents, seed


def test_inspection_streams_with_cancel_and_releases_pin(tmp_path, monkeypatch):
    import officeflow.infrastructure.data_inspection as module

    with documents(tmp_path) as (env, _creation):
        source = seed(env, b"x" * (5 * 1024 * 1024))
        repository = SqliteInspectionRepository(env.database)
        repository.scope()
        target = repository.current(source.asset_id)
        reads = []
        original_read = module.protected_read

        class RecordingStream:
            def __init__(self, stream):
                self.stream = stream

            def fileno(self):
                return self.stream.fileno()

            def read(self, size):
                reads.append(size)
                return self.stream.read(size)

        @contextmanager
        def recording(path):
            with original_read(path) as stream:
                yield RecordingStream(stream)

        monkeypatch.setattr(module, "protected_read", recording)
        reader = ManagedInspectionReader(env.attachments.storage_root, env.attachments.coordinator)
        reader.inspect(target, lambda: len(reads) >= 2)
        assert reads == [1024 * 1024, 1024 * 1024]
        assert not env.attachments.coordinator.is_pinned(source.relative_path)
        assert reader.inspect(target, lambda: False) == "normal"


def test_retention_failure_does_not_hide_published_backup(tmp_path, monkeypatch):
    from officeflow.bootstrap.paths import AppPaths
    from officeflow.infrastructure.database.migrate import upgrade_database

    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    manager = BackupManager(paths)

    def fail(_keep):
        raise PermissionError("private file")

    monkeypatch.setattr(manager, "prune_automatic_backups", fail)
    result = manager.create_backup(reason="automatic", keep=1)
    assert result.path.is_file()
    assert result.maintenance_warning
    assert manager.health.snapshot().attempts[0].status == "success"
    assert manager.health.snapshot().attention
    manager.verify_backup(result.path)
