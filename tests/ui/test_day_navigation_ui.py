from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QAbstractSlider, QVBoxLayout, QWidget

from officeflow.application.day_overview import DayOverviewResult
from officeflow.application.reminder_preview import ReminderPreviewItem, ReminderPreviewResult
from officeflow.application.tasks import ScheduledTask
from officeflow.domain.task import Task
from officeflow.presentation.day_calendar import DayCalendarPanel
from officeflow.presentation.theme import LIGHT_STYLESHEET

DAY = date(2026, 10, 20)


def schedule(hour, number=1, end_hour=None):
    at = datetime(2026, 10, 20, hour, tzinfo=UTC)
    end = at.replace(hour=end_hour) if end_hour is not None else None
    task = replace(Task.create(title=f"일정 {number}", starts_at=at, ends_at=end), id=number)
    return ScheduledTask(task, at, end)


def alarm(hour, number=1, status="예정"):
    value = schedule(hour, number)
    return ReminderPreviewItem(value.task, number, None, value.starts_at, value.starts_at, status)


@pytest.fixture
def panel(qtbot):
    owner = QWidget()
    owner.setObjectName("appRoot")  # Match the production main window's explicit light surface.
    owner.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
    owner.setStyleSheet(LIGHT_STYLESHEET)
    layout = QVBoxLayout(owner)
    panel = DayCalendarPanel("UTC", owner)
    panel.timeline.clock = lambda: datetime(2026, 10, 20, 17, tzinfo=UTC)
    layout.addWidget(panel)
    panel.set_day(DAY)
    qtbot.addWidget(owner)
    owner.resize(560, 480)
    owner.show()
    qtbot.wait(30)
    yield panel
    panel.shutdown()
    owner.close()


def load(panel, schedules=(), alarms=(), complete=True):
    panel.set_result(DayOverviewResult(DAY, schedules, complete))
    panel.set_reminders(ReminderPreviewResult(alarms, complete))


def test_today_opens_first_item_not_current_time_and_now_is_explicit(panel, qtbot):
    load(panel, (schedule(5), schedule(23, 2)))
    qtbot.waitUntil(lambda: 200 < panel.time_scroll.verticalScrollBar().value() < 320)
    assert panel.now_button.isVisible()
    panel.now_button.click()
    assert 800 < panel.time_scroll.verticalScrollBar().value() < 1000
    panel.set_day(DAY + timedelta(days=1))
    qtbot.waitUntil(panel.now_button.isHidden)


def test_empty_day_opens_nine_and_all_day_is_not_a_hidden_time_item(panel, qtbot):
    all_day = replace(schedule(0), task=replace(schedule(0).task, all_day=True))
    load(panel, (all_day,))
    qtbot.waitUntil(lambda: panel.time_scroll.verticalScrollBar().value() == 9 * 56)
    assert not panel.earlier_button.isEnabled() and not panel.later_button.isEnabled()
    assert panel.timeline.minimumHeight() >= 24 * 56


def test_late_alarm_source_refines_initial_position_but_refresh_keeps_user_scroll(panel, qtbot):
    panel.set_result(DayOverviewResult(DAY, (schedule(12),), True))
    qtbot.wait(30)
    panel.set_reminders(ReminderPreviewResult((alarm(2),), True))
    qtbot.waitUntil(lambda: panel.time_scroll.verticalScrollBar().value() < 150)
    bar = panel.time_scroll.verticalScrollBar()
    bar.triggerAction(QAbstractSlider.SliderAction.SliderPageStepAdd)
    user_position = bar.value()
    panel.set_reminders(ReminderPreviewResult((alarm(0),), True))
    qtbot.wait(30)
    assert bar.value() == user_position


def test_offscreen_counts_include_group_members_and_exclude_history(panel, qtbot):
    load(panel, tuple(schedule(1, i) for i in range(1, 7)), (alarm(23), alarm(22, 2, "확인함")))
    qtbot.wait(40)
    panel.time_scroll.verticalScrollBar().setValue(12 * 56)
    qtbot.waitUntil(lambda: "6건" in panel.earlier_button.text())
    assert "1건" in panel.later_button.text()
    assert "일정 6" in panel.earlier_button.toolTip()
    assert "알림 1" in panel.later_button.toolTip()
    activated = []
    panel.activated.connect(activated.append)
    panel.later_button.click()
    assert panel.time_scroll.verticalScrollBar().value() > 12 * 56
    panel.earlier_button.click()
    assert panel.time_scroll.verticalScrollBar().value() < 12 * 56
    assert not activated


def test_partially_visible_duration_is_not_counted_as_hidden(panel, qtbot):
    load(panel, (schedule(1, end_hour=23),))
    qtbot.wait(40)
    panel.time_scroll.verticalScrollBar().setValue(12 * 56)
    qtbot.wait(30)
    assert not panel.earlier_button.isEnabled() and not panel.later_button.isEnabled()


def test_partial_query_navigation_explains_counts_and_does_not_request_more(panel, qtbot):
    load(panel, (schedule(1), schedule(23, 2)), complete=False)
    qtbot.wait(30)
    panel.time_scroll.verticalScrollBar().setValue(12 * 56)
    qtbot.waitUntil(lambda: panel.earlier_button.isEnabled())
    assert "조회된" in panel.earlier_button.toolTip()
    requested = []
    panel.scheduleMoreRequested.connect(lambda: requested.append(True))
    panel.earlier_button.click()
    assert not requested


def test_keyboard_navigation_and_mode_return_keep_scroll(panel, qtbot):
    load(panel, (schedule(1), schedule(23, 2)))
    qtbot.wait(40)
    panel.now_button.click()
    at = panel.time_scroll.verticalScrollBar().value()
    panel.set_mode("alarms")
    panel.set_mode("day")
    qtbot.wait(30)
    assert panel.time_scroll.verticalScrollBar().value() == at
    qtbot.keyPress(panel.timeline, Qt.Key.Key_Down)
    assert panel._positioned == DAY


def test_current_time_button_uses_elapsed_time_on_dst_day(panel, qtbot):
    from zoneinfo import ZoneInfo

    day = date(2026, 11, 1)
    panel.timezone = panel.timeline.timezone = "America/New_York"
    panel.timeline.clock = lambda: datetime(
        2026, 11, 1, 1, 30, tzinfo=ZoneInfo(panel.timezone), fold=1
    )
    panel.set_day(day)
    panel.set_result(DayOverviewResult(day, (), True))
    panel.set_reminders(ReminderPreviewResult((), True))
    qtbot.wait(30)
    panel.now_button.click()
    assert panel.time_scroll.verticalScrollBar().value() == 84  # 2.5 elapsed hours minus 1.
    assert panel.timeline.minimumHeight() == 25 * 56 + 60


def test_midnight_hides_now_without_moving_user_position(panel, qtbot):
    load(panel, (schedule(1), schedule(23, 2)))
    qtbot.wait(30)
    panel.now_button.click()
    position = panel.time_scroll.verticalScrollBar().value()
    panel.timeline.clock = lambda: datetime(2026, 10, 21, tzinfo=UTC)
    panel._tick()
    qtbot.waitUntil(panel.now_button.isHidden)
    assert panel.time_scroll.verticalScrollBar().value() == position


def test_pending_edge_refresh_stays_stopped_after_shutdown(panel, qtbot):
    load(panel, (schedule(1),))
    panel.shutdown()
    panel.window().resize(580, 500)
    panel._queue_edges()
    qtbot.wait(30)
    assert not panel._edge_timer.isActive() and not panel._position_timer.isActive()


def test_dark_palette_scrollbar_pixels_and_navigation_fit(panel, qtbot, qapp):
    original = QPalette(qapp.palette())
    dark = QPalette(original)
    for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
        dark.setColor(role, QColor("#202020"))
    try:
        qapp.setPalette(dark)
        load(panel, (schedule(1), schedule(23, 2)), (alarm(2), alarm(22, 2)))
        qtbot.wait(60)
        bar = panel.time_scroll.verticalScrollBar()
        bar.setValue(12 * 56)
        qtbot.waitUntil(lambda: panel.earlier_button.isEnabled() and panel.later_button.isEnabled())
        image = bar.grab().toImage()
        colors = {
            image.pixelColor(x, y).name()
            for x in range(image.width())
            for y in range(image.height())
        }
        assert "#edf1f7" in colors
        assert colors.intersection({"#7fa3ef", "#4f7fdf"})
        for button in (panel.earlier_button, panel.later_button, panel.now_button):
            assert button.width() >= button.sizeHint().width()
            assert panel.rect().contains(button.mapTo(panel, button.rect().bottomRight()))
        assert panel.time_scroll.viewport().height() >= 150
        assert panel.window().grab().save(f"artifacts/day-nav-dark-{panel.devicePixelRatioF()}.png")
    finally:
        qapp.setPalette(original)
