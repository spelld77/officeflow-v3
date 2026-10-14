from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from officeflow.application.reminder_preview import ReminderPreviewItem, ReminderPreviewResult
from officeflow.application.tasks import CalendarOverview, ScheduledTask
from officeflow.domain.enums import TaskStatus
from officeflow.domain.task import Task
from officeflow.presentation.month_calendar import CalendarPage, MonthCalendarWidget
from officeflow.presentation.theme import LIGHT_STYLESHEET
from tests.unit.test_calendar_attention import scheduled


def test_day_list_completed_below_acknowledged_unfinished(qtbot):
    page = CalendarPage(timezone="UTC")
    qtbot.addWidget(page)
    page.setStyleSheet(LIGHT_STYLESHEET)
    page.resize(940, 720)
    page.show()
    page.calendar.set_month(2026, 10)
    page.calendar.set_selected_date(date(2026, 10, 14))
    done = scheduled(1, 14, status=TaskStatus.COMPLETED)
    active = scheduled(2, 14)
    page._day_tasks = (done, active)
    alarm = ReminderPreviewItem(active.task, 1, None, active.starts_at, active.starts_at, "확인함")
    page._reminder_preview = ReminderPreviewResult((alarm,), True)
    page._refresh_day_list()
    assert page.day_list.item(0).data(Qt.ItemDataRole.UserRole).id == 2
    assert page.day_list.item(0).foreground().color().name() == "#172033"
    assert page.day_list.item(1).foreground().color().name() == "#7d899e"
    page.day_list.setCurrentRow(0)
    page._day_tasks = (
        done,
        replace(active, task=replace(active.task, status=TaskStatus.COMPLETED)),
    )
    page._refresh_day_list(preserve_selection=True)
    assert page.day_list.currentItem().data(Qt.ItemDataRole.UserRole).id == 2


@pytest.mark.parametrize("navigate_away", [False, True])
def test_alarm_focus_waits_for_preview_and_does_not_follow_to_another_date(qtbot, navigate_away):
    from officeflow.application.reminder_groups import ReminderGroup

    start = datetime(2026, 10, 14, 9, tzinfo=UTC)
    task = replace(Task.create(title="Later alarm", starts_at=start + timedelta(days=5)), id=50)
    regular = tuple(
        ScheduledTask(
            replace(
                Task.create(title=f"Earlier {i}", starts_at=start - timedelta(minutes=10)), id=i
            ),
            start - timedelta(minutes=10),
            None,
        )
        for i in range(1, 9)
    )
    alarm = ReminderPreviewItem(task, 1, None, start, start, "예정")
    result = ReminderPreviewResult((alarm,), True)
    page = CalendarPage(timezone="UTC")
    qtbot.addWidget(page)
    page.resize(760, 560)
    page.show()
    page._set_month_and_date(start.date())
    page.calendar.alarmRequested.emit(start.date())
    page.set_day_tasks(regular)
    assert page._alarm_focus_date == start.date()
    if navigate_away:
        page._set_month_and_date(start.date() + timedelta(days=1))
    page.set_day_reminder_preview(start.date(), result)
    qtbot.wait(20)
    assert page._alarm_focus_date is None
    if navigate_away:
        assert page.day_list.currentRow() == -1
    else:
        assert page.day_list.currentRow() == 8
        assert isinstance(page.day_list.currentItem().data(Qt.ItemDataRole.UserRole), ReminderGroup)
        assert (
            page.day_list.viewport()
            .rect()
            .intersects(page.day_list.visualItemRect(page.day_list.currentItem()))
        )
        page.set_day_reminder_preview(start.date(), result)
        assert (
            page.day_list.viewport()
            .rect()
            .intersects(page.day_list.visualItemRect(page.day_list.currentItem()))
        )


@pytest.mark.parametrize("height", [285, 620])
def test_hidden_unfinished_emphasized_not_completed(qtbot, height):
    widget = MonthCalendarWidget(timezone="UTC")
    qtbot.addWidget(widget)
    widget.resize(980, height)
    widget.set_month(2026, 10)
    tasks = tuple(
        scheduled(index + 1, 14, status=TaskStatus.COMPLETED if index < 3 else TaskStatus.ACTIVE)
        for index in range(7)
    )
    widget.set_tasks(tasks)
    widget.show()
    widget.grab()
    label = widget._more_labels[date(2026, 10, 14)]
    assert "미완료" in label or "남음" in label or label in {"3", "4"}
    assert all(task.status == TaskStatus.ACTIVE for _, task in widget._task_hits)
    assert "미완료 4개" in widget._count_tooltip(date(2026, 10, 14))
    for task_rect, _ in widget._more_hits:
        assert not any(task_rect.intersects(alarm_rect) for alarm_rect, _ in widget._alarm_hits)
        assert not any(task_rect.intersects(bar_rect) for bar_rect, _ in widget._task_hits)


def test_partial_overview_does_not_claim_unfinished_zero(qtbot):
    widget = MonthCalendarWidget(timezone="UTC")
    qtbot.addWidget(widget)
    widget.resize(980, 620)
    widget.set_month(2026, 10)
    day = date(2026, 10, 14)
    widget.set_overview(
        CalendarOverview((scheduled(1, 14, status=TaskStatus.COMPLETED),), ((day, 61),), 61, False)
    )
    widget.grab()
    assert "목록 확인" in widget._more_labels[day]
    assert "상태 일부만 조회" in widget._count_tooltip(day)
    assert "미완료 0" not in widget._count_tooltip(day)


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("width", [540, 980])
def test_calendar_attention_render(qtbot, dark, width):
    app = QApplication.instance()
    previous = app.palette()
    palette = QPalette(previous)
    if dark:
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#202020"))
        for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text):
            palette.setColor(role, QColor("#dddddd"))
    app.setPalette(palette)
    try:
        page = CalendarPage(timezone="UTC")
        qtbot.addWidget(page)
        page.setStyleSheet(LIGHT_STYLESHEET)
        page.resize(width, 640)
        page.show()
        page.calendar.set_month(2026, 10)
        page.calendar.set_selected_date(date(2026, 10, 14))
        tasks = tuple(
            scheduled(
                index + 1, 14, status=TaskStatus.COMPLETED if index < 2 else TaskStatus.ACTIVE
            )
            for index in range(6)
        )
        page.calendar.set_tasks(tasks)
        page._day_tasks = tasks
        page._refresh_day_list()
        qtbot.wait(30)
        directory = Path("artifacts/u24-u25-captures")
        directory.mkdir(parents=True, exist_ok=True)
        import os

        assert page.grab().save(
            str(directory / f"calendar-{dark}-{width}-{os.getenv('QT_SCALE_FACTOR', '1')}.png")
        )
        assert "미완료" in page.day_count.text()
        assert not any(
            more_rect.intersects(bar_rect)
            for more_rect, _ in page.calendar._more_hits
            for bar_rect, _ in page.calendar._task_hits
        )
        assert all(
            page.day_list.item(i).data(Qt.ItemDataRole.UserRole).status == TaskStatus.ACTIVE
            for i in range(4)
        )
    finally:
        app.setPalette(previous)
