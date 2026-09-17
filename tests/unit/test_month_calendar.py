from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from officeflow.application.tasks import ScheduledTask
from officeflow.domain.enums import TaskPriority
from officeflow.domain.task import Task
from officeflow.presentation.month_calendar import (
    MonthCalendarWidget,
    build_calendar_segments,
    month_grid_start,
    task_date_span,
    tasks_for_date,
)


def _task(title: str, start: datetime, end: datetime | None, task_id: int = 1) -> ScheduledTask:
    task = replace(
        Task.create(title=title, all_day=True, starts_at=start, ends_at=end),
        id=task_id,
    )
    return ScheduledTask(task, start, end)


def test_month_grid_is_sunday_first_and_always_covers_six_weeks() -> None:
    start = month_grid_start(2026, 9)

    assert start == date(2026, 8, 30)
    assert start + timedelta(days=41) == date(2026, 10, 10)


def test_exclusive_end_is_rendered_as_inclusive_final_date() -> None:
    task = _task(
        "출장",
        datetime(2026, 9, 3, 15, 0, tzinfo=UTC),
        datetime(2026, 9, 10, 15, 0, tzinfo=UTC),
    )

    assert task_date_span(task, "Asia/Seoul") == (date(2026, 9, 4), date(2026, 9, 10))
    assert tasks_for_date((task,), date(2026, 9, 10), "Asia/Seoul") == (task,)
    assert tasks_for_date((task,), date(2026, 9, 11), "Asia/Seoul") == ()


def test_multiday_bar_splits_at_week_boundary_with_continuation_markers() -> None:
    task = _task(
        "주간 출장",
        datetime(2026, 9, 3, 15, 0, tzinfo=UTC),
        datetime(2026, 9, 10, 15, 0, tzinfo=UTC),
    )

    segments = build_calendar_segments((task,), month_grid_start(2026, 9), "Asia/Seoul")

    assert len(segments) == 2
    assert (segments[0].week, segments[0].start_column, segments[0].end_column) == (0, 5, 6)
    assert segments[0].continues_before is False
    assert segments[0].continues_after is True
    assert (segments[1].week, segments[1].start_column, segments[1].end_column) == (1, 0, 4)
    assert segments[1].continues_before is True
    assert segments[1].continues_after is False


def test_month_boundary_schedule_remains_visible_in_adjacent_cells() -> None:
    task = _task(
        "월말 점검",
        datetime(2026, 9, 28, 15, 0, tzinfo=UTC),
        datetime(2026, 10, 2, 15, 0, tzinfo=UTC),
    )

    segments = build_calendar_segments((task,), month_grid_start(2026, 9), "Asia/Seoul")

    assert len(segments) == 1
    assert (segments[0].start_column, segments[0].end_column) == (2, 5)


def test_clicking_task_keeps_focused_calendar_grid_visible(qtbot: QtBot) -> None:
    class FocusedMonthCalendarWidget(MonthCalendarWidget):
        def hasFocus(self) -> bool:
            return True

    scheduled = _task(
        "선택할 일정",
        datetime(2026, 9, 16, 15, 0, tzinfo=UTC),
        datetime(2026, 9, 17, 15, 0, tzinfo=UTC),
    )
    scheduled = ScheduledTask(
        replace(scheduled.task, priority=TaskPriority.ATTENTION),
        scheduled.starts_at,
        scheduled.ends_at,
    )
    calendar = FocusedMonthCalendarWidget(timezone="Asia/Seoul")
    qtbot.addWidget(calendar)
    calendar.resize(700, 420)
    calendar.set_month(2026, 9)
    calendar.set_tasks((scheduled,))
    calendar.show()
    calendar.grab()
    task_rect = calendar._task_hits[0][0]

    qtbot.mouseClick(
        calendar,
        Qt.MouseButton.LeftButton,
        pos=task_rect.center().toPoint(),
    )
    rendered = calendar.grab().toImage()

    assert rendered.pixelColor(10, calendar.height() - 10).name() != "#3b8d78"
