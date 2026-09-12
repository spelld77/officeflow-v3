from __future__ import annotations

from pathlib import Path

from officeflow.tools.benchmark_tasks import run_benchmark


def test_five_thousand_task_queries_meet_phase_three_budget(tmp_path: Path) -> None:
    results = run_benchmark(tmp_path / "benchmark.db", count=5_000, runs=3)

    assert {result.scenario for result in results} == {
        "today-groups",
        "filtered-page",
        "text-search",
    }
    assert max(result.maximum_ms for result in results) < 200
