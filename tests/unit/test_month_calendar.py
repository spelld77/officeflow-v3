from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from officeflow.application.tasks import CalendarOverview, ScheduledTask
from officeflow.domain.enums import TaskPriority
from officeflow.domain.task import Task
from officeflow.presentation.month_calendar import (
    CalendarPage,
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


def test_right_clicking_calendar_task_requests_context_menu(qtbot: QtBot) -> None:
    scheduled = _task(
        "첨부 일정",
        datetime(2026, 9, 16, 15, 0, tzinfo=UTC),
        datetime(2026, 9, 17, 15, 0, tzinfo=UTC),
    )
    scheduled = ScheduledTask(
        replace(scheduled.task, has_attachments=True),
        scheduled.starts_at,
        scheduled.ends_at,
    )
    calendar = MonthCalendarWidget(timezone="Asia/Seoul")
    qtbot.addWidget(calendar)
    calendar.resize(700, 420)
    calendar.set_month(2026, 9)
    calendar.set_tasks((scheduled,))
    calendar.show()
    calendar.grab()
    task_rect = calendar._task_hits[0][0]

    with qtbot.waitSignal(calendar.taskContextRequested) as blocker:
        qtbot.mouseClick(
            calendar,
            Qt.MouseButton.RightButton,
            pos=task_rect.center().toPoint(),
        )

    assert blocker.args[0] == scheduled
    assert scheduled.has_attachments


def test_calendar_day_list_marks_attachment_and_supports_context_menu(qtbot: QtBot) -> None:
    scheduled = _task(
        "계약 검토",
        datetime(2026, 9, 16, 15, 0, tzinfo=UTC),
        datetime(2026, 9, 17, 15, 0, tzinfo=UTC),
    )
    scheduled = ScheduledTask(
        replace(scheduled.task, has_attachments=True),
        scheduled.starts_at,
        scheduled.ends_at,
    )
    page = CalendarPage(timezone="Asia/Seoul")
    qtbot.addWidget(page)
    page.calendar.set_month(2026, 9)
    page.calendar.set_selected_date(date(2026, 9, 17))
    page.set_tasks((scheduled,))
    page.show()

    assert "첨부" in page.day_list.item(0).text()
    item_rect = page.day_list.visualItemRect(page.day_list.item(0))
    with qtbot.waitSignal(page.taskContextRequested) as blocker:
        page._show_day_item_context_menu(item_rect.center())

    assert blocker.args[0] == scheduled


def test_dense_calendar_uses_exact_count_summary(qtbot: QtBot) -> None:
    page = CalendarPage(timezone="Asia/Seoul")
    qtbot.addWidget(page)
    page.calendar.set_month(2026, 9)
    page.calendar.set_selected_date(date(2026, 9, 17))
    page.set_overview(
        CalendarOverview(
            preview_tasks=(),
            day_counts=((date(2026, 9, 17), 143),),
            total=143,
            summary_mode=True,
        )
    )
    page.resize(720, 500)
    page.show()
    page.calendar.grab()

    assert page.summary_label.isVisible()
    assert page.calendar._task_hits == []
    assert any(day == date(2026, 9, 17) for _rect, day in page.calendar._more_hits)
