from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from time import perf_counter

from officeflow.application.tasks import TaskQuery, TaskService, TaskSort, TaskView
from officeflow.domain.enums import TaskPriority, TaskStatus
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository
from officeflow.tools.sample_data import generate


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    scenario: str
    median_ms: float
    maximum_ms: float


def run_benchmark(
    database_file: Path,
    *,
    count: int = 5_000,
    runs: int = 7,
) -> tuple[BenchmarkResult, ...]:
    if database_file.exists():
        raise FileExistsError(f"벤치마크 DB가 이미 존재합니다: {database_file}")
    if count < 1:
        raise ValueError("벤치마크 업무 수는 1개 이상이어야 합니다.")
    if runs < 1:
        raise ValueError("벤치마크 반복 횟수는 1회 이상이어야 합니다.")

    generate(database_file, count)
    engine = create_database_engine(database_file)
    service = TaskService(SqlAlchemyTaskRepository(SessionFactory(engine)))
    now = datetime.now(UTC)
    scenarios: tuple[tuple[str, Callable[[], object]], ...] = (
        (
            "today-groups",
            lambda: service.today_groups(limit_per_group=50, now=now),
        ),
        (
            "filtered-page",
            lambda: service.query(
                TaskQuery(
                    view=TaskView.ALL,
                    statuses=frozenset({TaskStatus.ACTIVE}),
                    priorities=frozenset({TaskPriority.IMPORTANT, TaskPriority.URGENT}),
                    sort=TaskSort.PRIORITY,
                    limit=100,
                ),
                now=now,
            ),
        ),
        (
            "text-search",
            lambda: service.query(
                TaskQuery(view=TaskView.ALL, search="샘플 업무 4999", limit=100),
                now=now,
            ),
        ),
    )
    results: list[BenchmarkResult] = []
    try:
        for name, scenario in scenarios:
            scenario()
            elapsed: list[float] = []
            for _ in range(runs):
                started = perf_counter()
                scenario()
                elapsed.append((perf_counter() - started) * 1_000)
            results.append(
                BenchmarkResult(
                    scenario=name,
                    median_ms=round(median(elapsed), 2),
                    maximum_ms=round(max(elapsed), 2),
                )
            )
    finally:
        engine.dispose()
    return tuple(results)


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark OfficeFlow task queries")
    parser.add_argument("database", type=Path)
    parser.add_argument("--count", type=int, default=5_000)
    parser.add_argument("--runs", type=int, default=7)
    args = parser.parse_args()
    results = run_benchmark(args.database, count=args.count, runs=args.runs)
    print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
