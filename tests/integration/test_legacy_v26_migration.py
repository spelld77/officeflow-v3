from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from officeflow.application.migration import LegacyMigrationError
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.migration.legacy_v26 import LegacyV26Migration


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_database(path: Path, saved_files: Path) -> None:
    saved_files.mkdir(parents=True)
    (saved_files / "attached.txt").write_text("legacy attachment", encoding="utf-8")
    (path.parent / "related.pdf").write_bytes(b"related-file")
    with closing(sqlite3.connect(path)) as database:
        database.executescript(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY,
                content TEXT,
                alarm_time TEXT,
                target_date TEXT,
                file_path TEXT,
                frequency TEXT,
                week_day INTEGER,
                status TEXT,
                deadline TEXT,
                category TEXT,
                created_at TEXT,
                updated_at TEXT,
                completed_at TEXT,
                result_memo TEXT,
                weekday INTEGER
            );
            CREATE TABLE history_logs (
                id INTEGER PRIMARY KEY,
                task_id INTEGER,
                date TEXT,
                content TEXT,
                result TEXT,
                category TEXT
            );
            CREATE TABLE memos (id INTEGER PRIMARY KEY, content TEXT);
            CREATE TABLE attachments (
                id INTEGER PRIMARY KEY,
                task_id INTEGER,
                file_name TEXT,
                saved_path TEXT,
                upload_date TEXT
            );
            """
        )
        database.executemany(
            """
            INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    10,
                    "완료한 단일 업무\n원문의 두 번째 줄",
                    "09:30",
                    "2026-09-15",
                    "",
                    "Once",
                    None,
                    "done",
                    "2026-09-16",
                    "중요",
                    "2026-09-01 08:00:00",
                    "2026-09-15 10:00:00",
                    "2026-09-15 10:00:00",
                    "완료 결과",
                    None,
                ),
                (
                    20,
                    "매일 반복 업무",
                    "14:00",
                    "2026-09-14",
                    "related.pdf",
                    "Daily",
                    None,
                    "active",
                    "",
                    "긴급",
                    "2026-09-01 08:00:00",
                    "2026-09-14 14:00:00",
                    None,
                    "",
                    None,
                ),
                (
                    30,
                    "시간 없는 종일 업무",
                    "not-a-time",
                    "2026-09-20",
                    "missing-folder",
                    "Weekly",
                    6,
                    "pending",
                    "",
                    "관심",
                    "bad-date",
                    "",
                    None,
                    "",
                    6,
                ),
            ),
        )
        database.executemany(
            "INSERT INTO history_logs VALUES (?, ?, ?, ?, ?, ?)",
            (
                (1, 20, "2026-09-15", "반복 완료", "처리함", "긴급"),
                (2, 999, "bad-date", "고아 일지", "", "보통"),
            ),
        )
        database.execute("INSERT INTO memos VALUES (1, '기존 메모')")
        database.executemany(
            "INSERT INTO attachments VALUES (?, ?, ?, ?, ?)",
            (
                (1, 10, "attached.txt", "saved_files/attached.txt", "2026-09-01 09:00:00"),
                (2, 20, "missing.txt", "saved_files/missing.txt", "2026-09-01 09:00:00"),
                (3, 999, "orphan.txt", "saved_files/orphan.txt", "2026-09-01 09:00:00"),
            ),
        )
        database.commit()


def test_v26_preview_migrate_and_apply_preserves_source_and_converts_data(tmp_path) -> None:
    paths = AppPaths(tmp_path / "v3")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    with closing(sqlite3.connect(paths.database_file)) as database:
        database.execute(
            """
            INSERT INTO tasks (
                legacy_id, title, description, status, priority, is_pinned, all_day,
                starts_at, ends_at, timezone, recurrence_rule, recurrence_until,
                result_note, completed_at, created_at, updated_at, deleted_at
            ) VALUES (
                NULL, '기존 3.0 업무', '', 'active', 'normal', 0, 0,
                NULL, NULL, 'Asia/Seoul', NULL, NULL, '', NULL,
                '2026-09-01 00:00:00', '2026-09-01 00:00:00', NULL
            )
            """
        )
        database.commit()
    legacy = tmp_path / "legacy" / "office_tasks.db"
    legacy.parent.mkdir()
    _legacy_database(legacy, legacy.parent / "saved_files")
    original_digest = _digest(legacy)
    manager = BackupManager(paths)
    migration = LegacyV26Migration(paths, manager)

    preview = migration.preview(legacy)

    assert preview.can_import
    assert preview.source_counts.tasks == 3
    assert preview.importable_counts.tasks == 3
    assert preview.importable_counts.work_logs == 2
    assert preview.importable_counts.notes == 1
    assert preview.importable_counts.attachments == 3
    assert preview.missing_attachments == 1
    assert preview.orphan_work_logs == 1
    assert preview.orphan_attachments == 1
    assert {issue.code for issue in preview.issues} >= {
        "invalid_alarm_time",
        "invalid_created_at",
        "legacy_deadline_preserved",
        "missing_attachment",
        "orphan_attachment",
        "orphan_work_log",
        "related_path_preserved",
    }

    result = migration.migrate(preview)

    assert _digest(legacy) == original_digest
    assert result.source_backup.is_file()
    assert result.pending_restore.is_file()
    assert result.report_path.is_file()
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["source_sha256"] == original_digest
    assert report["imported_counts"] == {
        "tasks": 3,
        "work_logs": 2,
        "notes": 1,
        "attachments": 3,
    }

    manager.apply_pending_restore()
    with closing(sqlite3.connect(paths.database_file)) as database:
        database.row_factory = sqlite3.Row
        assert database.execute(
            "SELECT COUNT(*) FROM tasks WHERE title='기존 3.0 업무'"
        ).fetchone()[0] == 1
        tasks = database.execute(
            "SELECT * FROM tasks WHERE legacy_id IS NOT NULL ORDER BY legacy_id"
        ).fetchall()
        assert [row["legacy_id"] for row in tasks] == [10, 20, 30]
        assert tasks[0]["status"] == "completed"
        assert tasks[0]["priority"] == "important"
        assert "기존 마감일: 2026-09-16" in tasks[0]["description"]
        assert tasks[1]["recurrence_rule"] == "FREQ=DAILY;INTERVAL=1"
        assert "기존 관련 경로: related.pdf" in tasks[1]["description"]
        assert tasks[2]["all_day"] == 1
        assert tasks[2]["status"] == "pending"
        assert database.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 2
        assert database.execute("SELECT COUNT(*) FROM task_occurrences").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM work_logs").fetchone()[0] == 2
        assert database.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1
        attachments = database.execute(
            "SELECT original_name, relative_path, missing_at FROM attachments ORDER BY id"
        ).fetchall()
        assert len(attachments) == 3
        assert sum(row["missing_at"] is not None for row in attachments) == 1
        stored = [row for row in attachments if row["missing_at"] is None]
        assert all((paths.attachment_dir / row["relative_path"]).is_file() for row in stored)
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []

    repeated = migration.preview(legacy)
    assert not repeated.can_import
    assert "source_already_imported" in {issue.code for issue in repeated.issues}


def test_v26_preview_rejects_non_legacy_database(tmp_path) -> None:
    paths = AppPaths(tmp_path / "v3")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    invalid = tmp_path / "invalid.db"
    with closing(sqlite3.connect(invalid)) as database:
        database.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT)")
        database.commit()

    with pytest.raises(LegacyMigrationError, match="필수 열"):
        LegacyV26Migration(paths, BackupManager(paths)).preview(invalid)


def test_v26_migration_requires_new_preview_when_source_changes(tmp_path) -> None:
    paths = AppPaths(tmp_path / "v3")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    legacy = tmp_path / "legacy" / "office_tasks.db"
    legacy.parent.mkdir()
    _legacy_database(legacy, legacy.parent / "saved_files")
    migration = LegacyV26Migration(paths, BackupManager(paths))
    preview = migration.preview(legacy)
    with closing(sqlite3.connect(legacy)) as database:
        database.execute("INSERT INTO memos VALUES (2, '변경됨')")
        database.commit()

    with pytest.raises(LegacyMigrationError, match="변경"):
        migration.migrate(preview)


def test_minimal_v26_schema_imports_unscheduled_task(tmp_path) -> None:
    paths = AppPaths(tmp_path / "v3")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    legacy = tmp_path / "minimal.db"
    with closing(sqlite3.connect(legacy)) as database:
        database.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, content TEXT)")
        database.execute("INSERT INTO tasks VALUES (7, '최소 스키마 업무')")
        database.commit()
    manager = BackupManager(paths)
    migration = LegacyV26Migration(paths, manager)

    result = migration.migrate(migration.preview(legacy))
    manager.apply_pending_restore()

    assert result.imported_counts.tasks == 1
    with closing(sqlite3.connect(paths.database_file)) as database:
        row = database.execute(
            "SELECT legacy_id, title, starts_at FROM tasks"
        ).fetchone()
        assert row == (7, "최소 스키마 업무", None)


def test_attachment_removed_after_preview_is_reported_as_missing(tmp_path) -> None:
    paths = AppPaths(tmp_path / "v3")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    legacy = tmp_path / "legacy" / "office_tasks.db"
    legacy.parent.mkdir()
    saved_files = legacy.parent / "saved_files"
    _legacy_database(legacy, saved_files)
    migration = LegacyV26Migration(paths, BackupManager(paths))
    preview = migration.preview(legacy)
    (saved_files / "attached.txt").unlink()

    result = migration.migrate(preview)

    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert result.missing_attachments == 2
    assert report["missing_attachments"] == 2
    assert "missing_attachment_during_import" in {
        issue["code"] for issue in report["issues"]
    }


def test_report_failure_does_not_schedule_restore(tmp_path, monkeypatch) -> None:
    paths = AppPaths(tmp_path / "v3")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    legacy = tmp_path / "minimal.db"
    with closing(sqlite3.connect(legacy)) as database:
        database.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, content TEXT)")
        database.execute("INSERT INTO tasks VALUES (1, '업무')")
        database.commit()
    migration = LegacyV26Migration(paths, BackupManager(paths))
    preview = migration.preview(legacy)

    def fail_report(**_kwargs) -> Path:
        raise LegacyMigrationError("보고서 실패")

    monkeypatch.setattr(migration, "_write_report", fail_report)

    with pytest.raises(LegacyMigrationError, match="보고서 실패"):
        migration.migrate(preview)
    assert not paths.pending_restore_file.exists()
