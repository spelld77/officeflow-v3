from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from time import perf_counter

from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.migration.legacy_v26 import LegacyV26Migration


def test_five_thousand_legacy_tasks_migrate_within_budget(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "v3")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    legacy = tmp_path / "office_tasks.db"
    with closing(sqlite3.connect(legacy)) as database:
        database.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, content TEXT)")
        database.executemany(
            "INSERT INTO tasks VALUES (?, ?)",
            ((task_id, f"가져올 업무 {task_id}") for task_id in range(1, 5_001)),
        )
        database.commit()
    migration = LegacyV26Migration(paths, BackupManager(paths))

    started = perf_counter()
    preview = migration.preview(legacy)
    result = migration.migrate(preview)
    elapsed_seconds = perf_counter() - started

    assert result.imported_counts.tasks == 5_000
    assert elapsed_seconds < 30
