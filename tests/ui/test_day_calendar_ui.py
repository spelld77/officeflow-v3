from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette

from officeflow.application.day_overview import DayCursor
from officeflow.application.reminder_preview import ReminderPreviewItem, ReminderPreviewResult
from officeflow.application.tasks import TaskDraft
from officeflow.domain.enums import TaskStatus
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow
from officeflow.presentation.theme import LIGHT_STYLESHEET


@pytest.fixture
def day_window(qtbot, task_service):
    start = datetime(2027, 10, 15, 1, tzinfo=UTC)
    task_service.create(TaskDraft(title="시간만 지정", starts_at=start))
    task_service.create(
        TaskDraft(
            title="기간 일정",
            starts_at=start + timedelta(hours=2),
            ends_at=start + timedelta(hours=3),
        )
    )
    task_service.create(
        TaskDraft(
            title="종일 업무",
            all_day=True,
            starts_at=start.replace(hour=0),
            ends_at=(start + timedelta(days=1)).replace(hour=0),
        )
    )
    window = MainWindow(AppSettings(), task_service)
    window.setStyleSheet(LIGHT_STYLESHEET)
    qtbot.addWidget(window)
    window.resize(920, 720)
    window.show()
    window._show_calendar()
    window._calendar_page._select_date(date(2027, 10, 15))
    window._calendar_page.set_mode("day")
    qtbot.waitUntil(lambda: window._calendar_page.day_view._schedule_loaded)
    try:
        yield window
    finally:
        window.shutdown()
        window.close()


def test_day_view_uses_whole_calendar_area_and_returns_to_month(day_window, qtbot):
    page = day_window._calendar_page
    assert page.day_view.isVisible() and not page.calendar_splitter.isVisible()
    assert len(page.day_view.result.items) == 3
    assert "2027-10-15" in page.month_label.text()
    assert page.day_view.all_day.count() == 1
    page.view_button.click()
    assert page.mode == "month" and page.calendar_splitter.isVisible()
    assert page.selected_date == date(2027, 10, 15)
    page.view_button.click()
    assert page.mode == "day"


def test_whole_day_alarms_do_not_require_a_task_selection(day_window, qtbot):
    page = day_window._calendar_page
    task = page.day_view.result.items[0].task
    at = datetime(2027, 10, 15, 5, tzinfo=UTC)
    alarms = (
        ReminderPreviewItem(task, 1, None, at, at, "예정"),
        ReminderPreviewItem(task, 2, None, at + timedelta(hours=1), at + timedelta(hours=1), "再"),
    )
    alarms = (alarms[0], replace(alarms[1], status="재알림"))
    page.set_day_reminder_preview(page.selected_date, ReminderPreviewResult(alarms, True))
    page.day_alarm_button.click()
    assert page.mode == "alarms"
    assert page.day_view.entries.list.count() == 2
    assert "2건" in page.day_alarm_button.text()
    page.view_button.click()
    assert page.mode == "day"


def test_day_timeline_clicks_keep_schedule_identity(day_window, qtbot):
    panel = day_window._calendar_page.day_view
    canvas = panel.timeline
    qtbot.waitUntil(lambda: bool(canvas.hits))
    selected = []
    activated = []
    panel.selected.connect(selected.append)
    panel.activated.connect(activated.append)
    rect, mark = next((rect, mark) for rect, mark in canvas.hits if len(mark.values) == 1)
    qtbot.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
    qtbot.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
    assert selected[-1] == mark.values[0]
    assert activated[-1] == mark.values[0]


def test_alarm_history_and_full_list_page_are_bounded(day_window):
    page = day_window._calendar_page
    task = page.day_view.result.items[0].task
    at = datetime(2027, 10, 15, 5, tzinfo=UTC)
    items = tuple(ReminderPreviewItem(task, i, None, at, at, "예정") for i in range(230))
    completed = replace(items[0], reminder_id=9999, task=replace(task, status=TaskStatus.COMPLETED))
    page.set_day_reminder_preview(
        page.selected_date, ReminderPreviewResult((*items, completed), False)
    )
    page.set_mode("alarms")
    assert page.day_view.entries.list.count() == 100
    assert len(page.day_view.entries.entries) == 230
    page.day_view.history.setChecked(True)
    assert len(page.day_view.entries.entries) == 231


def test_schedule_cursor_pages_can_return_and_reset_timeline(day_window):
    panel = day_window._calendar_page.day_view
    first = panel.result
    cursor = DayCursor(999)
    second = replace(first, requested_cursor=cursor)
    panel.set_mode("schedule-list")
    panel.set_result(second)
    assert panel._page_index == 1 and panel.entries.page == 0
    requested = []
    panel.scheduleCursorRequested.connect(requested.append)
    panel.previous_schedule.click()
    assert requested[-1] is None
    panel._toggle_schedule_list()
    assert panel.mode == "day" and requested[-1] is None


def test_failed_alarm_query_is_not_hidden_by_successful_schedule_refresh(day_window):
    panel = day_window._calendar_page.day_view
    panel.set_error("알림 서버 상태", source="alarm")
    panel.set_result(panel.result)
    assert panel.notice.isVisible() and "알림" in panel.notice.text()
    panel.set_reminders(ReminderPreviewResult((), True))
    assert not panel.notice.isVisible()


def test_selected_day_detail_reflects_new_completion_state(day_window):
    panel = day_window._calendar_page.day_view
    value = panel.result.items[0]
    panel._select(value)
    completed = replace(value, task=replace(value.task, status=TaskStatus.COMPLETED))
    refreshed = replace(panel.result, items=(completed, *panel.result.items[1:]))
    panel.set_result(refreshed)
    assert "완료" in panel.detail.toolTip()


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("width", [760, 1180])
def test_day_view_small_window_palette_and_capture(day_window, qtbot, qapp, dark, width):
    original = QPalette(qapp.palette())
    if dark:
        palette = QPalette(original)
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#202020"))
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
        ):
            palette.setColor(role, QColor("#eeeeee"))
        qapp.setPalette(palette)
    try:
        day_window.resize(width, 560)
        page = day_window._calendar_page
        task = page.day_view.result.items[0].task
        at = datetime(2027, 10, 15, 1, tzinfo=UTC)
        alarms = tuple(
            ReminderPreviewItem(
                task,
                number,
                None,
                at + timedelta(hours=number),
                at + timedelta(hours=number),
                "재알림" if number == 1 else "예정",
            )
            for number in range(3)
        )
        page.set_day_reminder_preview(page.selected_date, ReminderPreviewResult(alarms, True))
        qtbot.wait(80)
        page = day_window._calendar_page
        assert page.day_view.time_scroll.viewport().height() >= 100
        assert page.day_view.time_scroll.horizontalScrollBar().maximum() == 0
        for button in (page.view_button, page.day_alarm_button, day_window._management_button):
            assert button.isVisible()
            assert day_window.rect().contains(button.mapTo(day_window, button.rect().bottomRight()))
        image = Path("artifacts") / f"day-overview-{width}-{'dark' if dark else 'light'}.png"
        assert day_window.grab().save(str(image))
        page.day_alarm_button.click()
        qtbot.wait(30)
        assert page.day_view.entries.list.count() == 3
        assert day_window.grab().save(
            str(Path("artifacts") / f"day-alarms-{width}-{'dark' if dark else 'light'}.png")
        )
        page.view_button.click()
        day_window._management_menu.popup(
            day_window._management_button.mapToGlobal(
                day_window._management_button.rect().topRight()
            )
        )
        qtbot.wait(30)
        assert day_window._management_menu.grab().save(
            str(Path("artifacts") / f"management-menu-{'dark' if dark else 'light'}.png")
        )
        day_window._management_menu.hide()
    finally:
        qapp.setPalette(original)
