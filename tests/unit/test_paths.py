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
    assert not (paths.root / "migration-reports").exists()


def test_directory_setup_preserves_existing_import_reports(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "officeflow")
    report_dir = paths.root / "migration-reports"
    report_dir.mkdir(parents=True)
    report = report_dir / "existing-import.json"
    report.write_text('{"imported": true}', encoding="utf-8")

    paths.ensure_directories()

    assert report.read_text(encoding="utf-8") == '{"imported": true}'
