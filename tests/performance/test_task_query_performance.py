from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from time import perf_counter

from pytestqt.qtbot import QtBot

from officeflow.application.tasks import TaskService
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow
from officeflow.tools.benchmark_tasks import run_benchmark
from officeflow.tools.sample_data import generate


def test_five_thousand_task_queries_meet_phase_three_budget(tmp_path: Path) -> None:
    results = run_benchmark(tmp_path / "benchmark.db", count=5_000, runs=3)

    assert {result.scenario for result in results} == {
        "today-groups",
        "filtered-page",
        "text-search",
    }
    assert max(result.maximum_ms for result in results) < 200


def test_five_thousand_task_window_loads_bounded_first_page(qtbot: QtBot, tmp_path: Path) -> None:
    database_file = tmp_path / "ui-benchmark.db"
    generate(database_file, 5_000)
    engine = create_database_engine(database_file)
    service = TaskService(SqlAlchemyTaskRepository(SessionFactory(engine)))

    started = perf_counter()
    window = MainWindow(AppSettings(), service)
    elapsed_ms = (perf_counter() - started) * 1_000
    qtbot.addWidget(window)

    assert elapsed_ms < 500
    assert window._task_model.loaded_task_count <= 200
    assert window._task_model.total_task_count > window._task_model.loaded_task_count
    engine.dispose()


def test_five_thousand_task_calendar_range_meets_budget(tmp_path: Path) -> None:
    database_file = tmp_path / "calendar-benchmark.db"
    generate(database_file, 5_000)
    engine = create_database_engine(database_file)
    service = TaskService(SqlAlchemyTaskRepository(SessionFactory(engine)))
    start = date.today() - timedelta(days=14)

    started = perf_counter()
    tasks = service.calendar_range(start, start + timedelta(days=42))
    elapsed_ms = (perf_counter() - started) * 1_000

    assert len(tasks) > 0
    assert elapsed_ms < 200
    engine.dispose()
