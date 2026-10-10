import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from officeflow.application.reminder_groups import ReminderGroup
from officeflow.application.reminder_preview import ReminderPreviewItem, ReminderPreviewResult
from officeflow.application.tasks import ScheduledTask
from officeflow.domain.task import Task
from officeflow.presentation.month_calendar import CalendarPage
from officeflow.presentation.reminder_preview import ReminderPreviewWidget
from officeflow.presentation.theme import LIGHT_STYLESHEET
from tests.ui.test_multiple_reminders_ui import live_window as live_window

NOW = datetime(2026, 10, 10, 1, tzinfo=UTC)


def data(count=5):
    task = replace(Task.create(title="제출 준비", starts_at=NOW), id=1)
    items = tuple(
        ReminderPreviewItem(
            task, i + 1, None, NOW + timedelta(minutes=i), NOW + timedelta(minutes=i), "예정"
        )
        for i in range(count)
    )
    return task, ReminderPreviewResult(items, True)


def test_calendar_schedule_and_five_alarms_are_one_row(qtbot):
    task, result = data()
    page = CalendarPage(timezone="UTC")
    qtbot.addWidget(page)
    page._set_month_and_date(NOW.date())
    page.set_tasks((ScheduledTask(task, NOW, None),))
    page.set_reminder_preview(result)
    assert page.day_list.count() == 1
    assert "업무 1개" in page.day_count.text() and "알림 5건" in page.day_count.text()
    page.day_list.setCurrentRow(0)
    assert page._selected_task.id == task.id
    assert page.reminder_details_button.isEnabled()
    page.reminder_details_button.click()
    assert page._reminder_details.list.count() == 5
    page.close()


def test_today_five_alarms_do_not_use_five_rows_and_details_are_bounded(qtbot):
    _task, result = data(250)
    widget = ReminderPreviewWidget("UTC")
    qtbot.addWidget(widget)
    widget.set_result(result)
    assert widget.list.count() == 1
    group = widget.list.item(0).data(Qt.ItemDataRole.UserRole)
    assert isinstance(group, ReminderGroup) and len(group.items) == 250
    widget.list.setCurrentRow(0)
    widget.details_button.click()
    assert widget._details.list.count() == 100
    widget._details.next_button.click()
    assert widget._details.list.count() == 100
    widget._details.next_button.click()
    assert widget._details.list.count() == 50
    widget.shutdown()
    assert widget._details is None


def test_late_schedule_merges_into_selected_alarm_group(qtbot):
    task, result = data()
    page = CalendarPage(timezone="UTC")
    qtbot.addWidget(page)
    page._set_month_and_date(NOW.date())
    page.set_reminder_preview(result)
    page.day_list.setCurrentRow(0)
    page.reminder_details_button.click()
    dialog = page._reminder_details
    page.set_day_tasks((ScheduledTask(task, NOW, None),))
    assert page.day_list.count() == 1 and page.day_list.currentRow() == 0
    assert page._selected_task.starts_at == NOW
    assert dialog.list.count() == 5
    page.set_day_reminder_preview(NOW.date(), ReminderPreviewResult((), True))
    assert dialog.list.count() == 0 and not dialog.records_button.isEnabled()
    page.set_day_reminder_preview(NOW.date(), result)
    assert dialog.list.count() == 5
    page.shutdown()
    assert page._reminder_details is None


def test_today_all_groups_page_and_partial_lookup_do_not_create_unbounded_qt_rows(qtbot):
    task, result = data(1)
    items = tuple(
        replace(result.items[0], task=replace(task, id=i + 1), reminder_id=i + 1)
        for i in range(250)
    )
    widget = ReminderPreviewWidget("UTC")
    qtbot.addWidget(widget)
    widget.setStyleSheet(LIGHT_STYLESHEET)
    widget.set_result(ReminderPreviewResult(items, False))
    assert widget.list.count() == 3
    assert "이상" in widget.label.text() and widget.more.text() == "더 조회…"
    with qtbot.waitSignal(widget.reloadRequested):
        widget._open_all()
    from PySide6.QtWidgets import QListWidget

    full = widget._dialog.findChild(QListWidget)
    assert full.count() == 100
    widget._all_next.click()
    assert full.count() == 100
    widget._all_next.click()
    assert full.count() == 50
    widget.set_result(ReminderPreviewResult(items[:2], True))
    assert full.count() == 2 and widget._all_page == 0
    widget.shutdown()


def test_group_details_refresh_does_not_acknowledge_and_keeps_alarm_selection(qtbot):
    task, result = data()
    widget = ReminderPreviewWidget("UTC")
    qtbot.addWidget(widget)
    widget.set_result(result)
    widget.list.setCurrentRow(0)
    widget.details_button.click()
    dialog = widget._details
    dialog.list.setCurrentRow(2)
    selected = dialog.list.currentItem().data(Qt.ItemDataRole.UserRole).key
    updated = replace(result.items[0], status="확인함")
    widget.set_result(ReminderPreviewResult((updated, *result.items[1:]), True))
    assert dialog.list.currentItem().data(Qt.ItemDataRole.UserRole).key == selected
    assert "확인함" in dialog.list.item(0).text()
    with qtbot.waitSignal(widget.recordRequested) as signal:
        dialog.records_button.click()
    assert signal.args[0].key == selected and signal.args[0].task.id == task.id
    widget.shutdown()


def test_calendar_group_activates_exact_moved_occurrence_and_attachments(qtbot):
    task, result = data(2)
    task = replace(task, recurrence_rule="FREQ=DAILY", has_attachments=True)
    original = NOW - timedelta(days=2)
    moved = ScheduledTask(task, NOW, None, occurrence_start=original)
    alarms = tuple(replace(a, task=task, occurrence_start=original) for a in result.items)
    page = CalendarPage(timezone="UTC")
    qtbot.addWidget(page)
    page._set_month_and_date(NOW.date())
    page.set_day_tasks((moved,))
    page.set_reminder_preview(ReminderPreviewResult(alarms, True))
    assert page.day_list.count() == 1
    page.day_list.setCurrentRow(0)
    with qtbot.waitSignal(page.taskActivated) as signal:
        page._on_day_item_activated(page.day_list.item(0))
    assert signal.args[0].occurrence_start == original
    with qtbot.waitSignal(page.attachmentsRequested) as signal:
        page.attachments_button.click()
    assert signal.args[0] is moved


def test_calendar_snoozed_alarm_history_keeps_original_day_and_new_day(qtbot):
    _task, result = data(1)
    tomorrow = NOW + timedelta(days=1)
    alarm = replace(result.items[0], effective_at=tomorrow, snoozed_until=tomorrow, status="재알림")
    page = CalendarPage(timezone="UTC")
    qtbot.addWidget(page)
    page._set_month_and_date(NOW.date())
    page.set_reminder_preview(ReminderPreviewResult((alarm,), True))
    assert page.day_list.count() == 1 and "처리 이력" in page.day_list.item(0).text()
    assert page.calendar._alarm_counts[NOW.date()] == 0
    page._select_date(tomorrow.date())
    assert page.day_list.count() == 1 and "다음" in page.day_list.item(0).text()
    assert page.calendar._alarm_counts[tomorrow.date()] == 1


def test_completed_group_shows_scope_separately_from_each_alarm_state(qtbot):
    from officeflow.domain.enums import TaskStatus

    task, result = data(2)
    task = replace(task, status=TaskStatus.COMPLETED, completed_at=NOW)
    items = (
        replace(result.items[0], task=task, status="확인함"),
        replace(result.items[1], task=task, status="완료"),
    )
    widget = ReminderPreviewWidget("UTC")
    qtbot.addWidget(widget)
    widget.set_result(ReminderPreviewResult(items, True))
    assert "업무 완료" in widget.list.item(0).text()
    widget.list.setCurrentRow(0)
    widget.details_button.click()
    texts = [widget._details.list.item(i).text() for i in range(2)]
    assert "알림 확인함" in texts[0] and "알림 처리됨" in texts[1]
    widget.shutdown()


def test_other_task_gets_preview_space_not_another_alarm_for_first_task(qtbot):
    task, result = data(20)
    other = replace(result.items[0], task=replace(task, id=2, title="다른 업무"), reminder_id=30)
    widget = ReminderPreviewWidget("UTC")
    qtbot.addWidget(widget)
    widget.set_compact(True)
    widget.set_result(ReminderPreviewResult((*result.items, other), True))
    assert widget.list.count() == 2
    assert {widget.list.item(i).data(Qt.ItemDataRole.UserRole).id for i in range(2)} == {1, 2}


def test_calendar_unscheduled_alarm_detail_does_not_invent_task_schedule(live_window, qtbot):
    from zoneinfo import ZoneInfo

    from officeflow.application.task_saving import ReminderEdit
    from officeflow.application.tasks import TaskDraft
    from officeflow.domain.enums import ReminderRelation
    from officeflow.domain.reminder import ReminderRuleInput

    window, _env, _task, now, _repo = live_window
    task = window._task_save_service.save(
        TaskDraft("일정 없이 알림"),
        edits=(ReminderEdit(ReminderRuleInput(ReminderRelation.ABSOLUTE, absolute_at=now)),),
        now=now,
    )
    window._show_calendar()
    page = window._calendar_page
    page._set_month_and_date(now.astimezone(ZoneInfo("Asia/Seoul")).date())
    qtbot.waitUntil(
        lambda: any(
            page.day_list.item(i).data(Qt.ItemDataRole.UserRole).id == task.id
            for i in range(page.day_list.count())
        ),
        timeout=5000,
    )
    for i in range(page.day_list.count()):
        if page.day_list.item(i).data(Qt.ItemDataRole.UserRole).id == task.id:
            page.day_list.setCurrentRow(i)
            break
    from officeflow.presentation.task_list import format_task_schedule

    assert window._detail_schedule.text().startswith(format_task_schedule(task))
    assert format_task_schedule(task) == "일정 없음"
    page.reminder_details_button.click()
    page._reminder_details.settings_button.click()
    from officeflow.presentation.reminder_settings import ReminderSettingsDialog

    assert any(
        isinstance(d, ReminderSettingsDialog) and d._task.id == task.id
        for d in window._file_dialogs
    )


def test_long_unbroken_title_does_not_expand_alarm_details(qtbot):
    task, result = data()
    task = replace(task, title="W" * 200)
    widget = ReminderPreviewWidget("UTC")
    qtbot.addWidget(widget)
    widget.set_result(
        ReminderPreviewResult(tuple(replace(i, task=task) for i in result.items), True)
    )
    widget.list.setCurrentRow(0)
    widget.details_button.click()
    dialog = widget._details
    dialog.resize(460, 340)
    qtbot.wait(10)
    assert dialog.width() == 460
    assert dialog.rect().contains(
        dialog.close_button.mapTo(dialog, dialog.close_button.rect().bottomRight())
    )
    widget.shutdown()


def test_low_calendar_task_and_alarm_count_hit_regions_do_not_overlap(qtbot):
    from officeflow.presentation.month_calendar import MonthCalendarWidget

    task, _result = data()
    calendar = MonthCalendarWidget(timezone="UTC")
    qtbot.addWidget(calendar)
    calendar.set_month(NOW.year, NOW.month)
    calendar.set_compact(True)
    calendar.setMinimumHeight(146)
    calendar.resize(622, 146)
    calendar.set_tasks((ScheduledTask(task, NOW, None),))
    calendar.set_reminder_counts({NOW.date(): 12})
    calendar.show()
    calendar.grab()
    task_hit = next(rect for rect, day in calendar._more_hits if day == NOW.date())
    alarm_hit = next(rect for rect, day in calendar._alarm_hits if day == NOW.date())
    assert not task_hit.intersects(alarm_hit)
    with qtbot.waitSignal(calendar.alarmRequested) as signal:
        qtbot.mouseClick(calendar, Qt.MouseButton.LeftButton, pos=alarm_hit.center().toPoint())
    assert signal.args[0] == NOW.date()


@pytest.mark.parametrize("scale", ["1", "1.5", "2"])
@pytest.mark.parametrize("palette", ["light", "dark"])
def test_reliability_group_narrow_render(scale, palette):
    output = (
        Path(__file__).resolve().parents[2]
        / "artifacts"
        / "reliability-phase3-visual"
        / f"{palette}-{scale}"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.ui.reliability_phase3_visual_child",
            str(output),
            scale,
            palette,
        ],
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        capture_output=True,
        timeout=35,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
