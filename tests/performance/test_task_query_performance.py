from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import perf_counter

from pytestqt.qtbot import QtBot
from sqlalchemy import select

from officeflow.application.reminders import ReminderService
from officeflow.application.tasks import TaskService
from officeflow.infrastructure.database.models import ReminderRecord, TaskRecord
from officeflow.infrastructure.database.reminder_repository import SqlAlchemyReminderRepository
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


def test_five_thousand_task_calendar_overview_is_bounded_and_meets_budget(tmp_path: Path) -> None:
    database_file = tmp_path / "calendar-benchmark.db"
    generate(database_file, 5_000)
    engine = create_database_engine(database_file)
    service = TaskService(SqlAlchemyTaskRepository(SessionFactory(engine)))
    start = date.today() - timedelta(days=14)

    started = perf_counter()
    overview = service.calendar_overview(start, start + timedelta(days=42))
    elapsed_ms = (perf_counter() - started) * 1_000

    assert overview.total > 0
    assert len(overview.preview_tasks) <= service.CALENDAR_PREVIEW_LIMIT
    assert len(overview.day_counts) == 42
    assert elapsed_ms < 200
    engine.dispose()


def test_five_thousand_cached_reminders_query_only_due_rule(tmp_path: Path) -> None:
    database_file = tmp_path / "reminder-benchmark.db"
    generate(database_file, 5_000)
    engine = create_database_engine(database_file)
    sessions = SessionFactory(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    with sessions.transaction() as session:
        task_ids = tuple(
            session.scalars(
                select(TaskRecord.id).where(TaskRecord.status == "active")
            ).all()
        )
        for index, task_id in enumerate(task_ids):
            session.add(
                ReminderRecord(
                    task_id=task_id,
                    relation="start",
                    offset_minutes=0,
                    absolute_at=None,
                    enabled=True,
                    last_fired_key=None,
                    next_fire_at=now if index == 0 else now + timedelta(days=365),
                    next_occurrence_start=None,
                    schedule_initialized=True,
                )
            )
    task_service = TaskService(SqlAlchemyTaskRepository(sessions))
    service = ReminderService(SqlAlchemyReminderRepository(sessions), task_service)

    started = perf_counter()
    alerts = service.poll_due(now=now)
    elapsed_ms = (perf_counter() - started) * 1_000

    assert len(alerts) == 1
    assert elapsed_ms < 200
    engine.dispose()
