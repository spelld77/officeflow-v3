from __future__ import annotations

import argparse
import json
import math
import sqlite3
import tracemalloc
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from statistics import median
from time import perf_counter

from alembic import command
from sqlalchemy import insert

from officeflow.application.attachment_search import AttachmentSearchHit, AttachmentSearchQuery
from officeflow.application.tasks import TaskQuery, TaskService, TaskView
from officeflow.infrastructure.database.attachment_search_repository import (
    SqlAlchemyAttachmentSearchRepository,
)
from officeflow.infrastructure.database.migrate import migration_config, upgrade_database
from officeflow.infrastructure.database.models import AttachmentRecord
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository
from officeflow.tools.sample_data import generate


def benchmark(
    database: Path, *, tasks: int = 5000, attachments: int = 50000, runs: int = 20
) -> dict[str, object]:
    """Synthetic metadata only. Never modifies an existing database or real files."""
    if database.exists():
        raise FileExistsError("Benchmark requires a NEW database path")
    if tasks < 1 or attachments < 1 or runs < 1:
        raise ValueError("Counts must be positive")
    generate(database, tasks)
    command.downgrade(migration_config(database), "0009_reminder_schedule_cache")
    engine = create_database_engine(database)
    sessions = SessionFactory(engine)
    now = datetime.now(UTC)
    try:
        with sessions.transaction() as session:
            for start in range(0, attachments, 1000):
                session.execute(
                    insert(AttachmentRecord),
                    [
                        dict(
                            task_id=i % tasks + 1,
                            original_name=f"장비견적서_최종_{i:06d}.xlsx"
                            if i % 1000 == 0
                            else f"월간업무보고서_최종_{i:06d}.pdf",
                            stored_name=f"synthetic-{i}",
                            relative_path=f"synthetic/{i}",
                            size_bytes=1024,
                            created_at=now - timedelta(seconds=i),
                        )
                        for i in range(start, min(start + 1000, attachments))
                    ],
                )
    finally:
        engine.dispose()
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = database.stat().st_size
    started = perf_counter()
    upgrade_database(database)
    index_seconds = perf_counter() - started
    after = database.stat().st_size
    engine = create_database_engine(database)
    sessions = SessionFactory(engine)
    repo = SqlAlchemyAttachmentSearchRepository(sessions)
    service = TaskService(SqlAlchemyTaskRepository(sessions))
    measured = {}

    def measure(operation: Callable[[], object]) -> dict[str, float]:
        start = perf_counter()
        operation()
        first = (perf_counter() - start) * 1000
        values = []
        for _ in range(runs):
            start = perf_counter()
            operation()
            values.append((perf_counter() - start) * 1000)
        return {
            "first_ms": round(first, 2),
            "median_ms": round(median(values), 2),
            "p95_ms": round(sorted(values)[math.ceil(0.95 * runs) - 1], 2),
        }

    try:
        for term in ("없는문서", "견적서", "보고서", "보고", "견적", ""):
            measured[term or "recent"] = measure(
                partial(repo.search_page, AttachmentSearchQuery(search=term))
            )
        measured["task_list_보고서"] = measure(
            lambda: service.query(TaskQuery(view=TaskView.ALL, search="보고서", limit=50))
        )
        tracemalloc.start()
        retained: list[AttachmentSearchHit] = []
        cursor = None
        for _ in range(10):
            page = repo.search_page(AttachmentSearchQuery(search="보고서", cursor=cursor))
            retained.extend(page.items)
            cursor = page.next_cursor
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return {
            "tasks": tasks,
            "attachments": attachments,
            "runs": runs,
            "index_and_protection_backup_seconds": round(index_seconds, 2),
            "database_before_bytes": before,
            "database_after_bytes": after,
            "index_growth_bytes": after - before,
            "query_timings": measured,
            "retained_metadata_rows": len(retained),
            "python_allocated_bytes": current,
            "python_peak_bytes": peak,
            "memory_scope": "Python allocations during 500-row retrieval, not total process RSS",
        }
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark attachment search with isolated synthetic metadata"
    )
    parser.add_argument("database", type=Path)
    parser.add_argument("--tasks", type=int, default=5000)
    parser.add_argument("--attachments", type=int, default=50000)
    parser.add_argument("--runs", type=int, default=20)
    args = parser.parse_args()
    print(
        json.dumps(
            benchmark(
                args.database, tasks=args.tasks, attachments=args.attachments, runs=args.runs
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
