from __future__ import annotations

from pathlib import Path

from officeflow.bootstrap.paths import AppPaths


def test_app_paths_are_contained_in_explicit_root(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "officeflow")
    paths.ensure_directories()

    assert paths.database_file == paths.root / "data" / "officeflow.db"
    assert paths.attachment_dir.is_dir()
    assert paths.backup_dir.is_dir()
    assert paths.pending_restore_file == paths.root / "pending-restore.ofbackup"
    assert paths.log_dir.is_dir()
