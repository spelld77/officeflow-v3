"""Calendar controls reserve their real styled text and row bounds."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionButton, QVBoxLayout, QWidget

from officeflow.application.day_overview import DayOverviewResult
from officeflow.application.reminder_preview import ReminderPreviewItem, ReminderPreviewResult
from officeflow.application.tasks import ScheduledTask, TaskDraft
from officeflow.domain.task import Task
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow
from officeflow.presentation.month_calendar import CalendarPage
from officeflow.presentation.theme import LIGHT_STYLESHEET

DAY = date(2026, 10, 17)
START = datetime(2026, 10, 17, tzinfo=UTC)


@pytest.fixture
def calendar(qtbot):
    owner = QWidget()
    owner.setObjectName("appRoot")
    owner.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
    owner.setStyleSheet(LIGHT_STYLESHEET)
    layout = QVBoxLayout(owner)
    page = CalendarPage(timezone="UTC")
    layout.addWidget(page)
    qtbot.addWidget(owner)
    owner.resize(700, 720)
    owner.show()
    page._set_month_and_date(DAY)
    page.set_compact(True)
    qtbot.wait(30)
    yield owner, page
    page.day_view.shutdown()


def contents(button):
    option = QStyleOptionButton()
    button.initStyleOption(option)
    return button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("mode", ["month", "day", "alarms"])
def test_calendar_buttons_and_date_fit_styled_contents(calendar, qtbot, compact, mode):
    _owner, page = calendar
    page.set_compact(compact)
    page.set_mode(mode)
    qtbot.wait(20)
    for button in page.findChildren(QPushButton):
        if not button.isVisible() or not button.text():
            continue
        rect = contents(button)
        metrics = button.fontMetrics()
        assert rect.height() >= metrics.height(), (button.text(), rect, metrics.height())
        assert rect.width() >= metrics.horizontalAdvance(button.text()), (button.text(), rect)
    assert page.month_label.width() >= page.month_label.fontMetrics().horizontalAdvance(
        page.month_label.text()
    )


def sample_items(count):
    values = []
    for i in range(count):
        task = replace(
            Task.create(
                title=f"종일 일정 {i + 1}",
                starts_at=START,
                ends_at=START + timedelta(days=1),
                all_day=True,
            ),
            id=i + 1,
        )
        values.append(ScheduledTask(task, START, START + timedelta(days=1)))
    return tuple(values)


@pytest.mark.parametrize("count", [1, 2, 3])
def test_all_day_shows_two_whole_rows_not_one_and_a_fragment(calendar, qtbot, count):
    _owner, page = calendar
    page.set_mode("day")
    page.day_view.set_result(DayOverviewResult(DAY, sample_items(count), True))
    qtbot.wait(30)
    listing = page.day_view.all_day
    assert listing.count() == min(count, 2)
    for row in range(listing.count()):
        rect = listing.visualItemRect(listing.item(row))
        assert listing.viewport().rect().contains(rect), (
            row,
            rect,
            listing.viewport().rect(),
            listing.sizeHintForRow(row),
        )
    assert listing.verticalScrollBar().maximum() == 0
    assert page.day_view.all_day_more.isVisible() == (count > 2)


def test_calendar_measurements_do_not_grow_each_refresh(calendar, qtbot):
    owner, page = calendar
    page.set_mode("day")
    qtbot.wait(20)
    initial = owner.minimumSizeHint()
    heights = tuple(b.height() for b in (page.previous_button, page.next_button, page.today_button))
    for _ in range(15):
        page.set_compact(True)
        page._update_month_label()
    qtbot.wait(20)
    assert owner.minimumSizeHint() == initial
    assert (
        tuple(b.height() for b in (page.previous_button, page.next_button, page.today_button))
        == heights
    )


def test_all_day_rows_resize_with_font_change(calendar, qtbot):
    _owner, page = calendar
    page.set_mode("day")
    page.day_view.set_result(DayOverviewResult(DAY, sample_items(2), True))
    listing = page.day_view.all_day
    listing.setStyleSheet("QListWidget#dayOverviewList {font-size: 20px;}")
    qtbot.wait(30)
    for row in range(2):
        assert listing.viewport().rect().contains(listing.visualItemRect(listing.item(row)))


def test_calendar_inside_small_main_window(qtbot, task_service):
    for value in sample_items(3):
        task_service.create(
            TaskDraft(value.title, starts_at=START, ends_at=START + timedelta(days=1), all_day=True)
        )
    task_service.create(TaskDraft("13시 일정", starts_at=START + timedelta(hours=13)))
    window = MainWindow(AppSettings(), task_service, view_clock=lambda: START + timedelta(hours=13))
    qtbot.addWidget(window)
    window.resize(760, 800)
    window.show()
    try:
        window._show_calendar()
        page = window._calendar_page
        page._set_month_and_date(DAY)
        page.set_mode("day")
        qtbot.waitUntil(lambda: page.day_view._schedule_loaded)
        qtbot.wait(40)
        assert window.width() == 760
        for button in (
            page.today_button,
            page.previous_button,
            page.next_button,
            page.view_button,
            page.day_alarm_button,
        ):
            rect = contents(button)
            assert rect.height() >= button.fontMetrics().height()
            assert rect.width() >= button.fontMetrics().horizontalAdvance(button.text())
        listing = page.day_view.all_day
        assert listing.viewport().rect().contains(listing.visualItemRect(listing.item(1)))
        assert window.grab().save(
            f"artifacts/calendar-main-clipping-{window.devicePixelRatioF()}.png"
        )
    finally:
        window.shutdown()


@pytest.mark.parametrize("dark", [False, True])
def test_calendar_compact_visual_and_other_controls(calendar, qtbot, qapp, dark):
    owner, page = calendar
    original = QPalette(qapp.palette())
    palette = QPalette(original)
    if dark:
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#202020"))
    qapp.setPalette(palette)
    try:
        page.set_mode("day")
        task = replace(
            Task.create(title="13시 시간 일정", starts_at=START + timedelta(hours=13)), id=4
        )
        timed = ScheduledTask(task, task.starts_at, None)
        page.day_view.set_result(DayOverviewResult(DAY, (*sample_items(3), timed), True))
        alarms = tuple(
            ReminderPreviewItem(
                task,
                i + 1,
                None,
                START + timedelta(hours=9 + i),
                START + timedelta(hours=9 + i),
                "예정",
            )
            for i in range(3)
        )
        page.set_day_reminder_preview(DAY, ReminderPreviewResult(alarms, True))
        qtbot.wait(50)
        assert page.day_view.schedule_list_button.isVisible()
        assert page.day_view.time_scroll.viewport().height() > 200
        assert owner.grab().save(
            f"artifacts/calendar-clipping-{dark}-{owner.devicePixelRatioF()}.png"
        )
    finally:
        qapp.setPalette(original)
