from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from officeflow.application.task_saving import ReminderEdit
from officeflow.application.tasks import TaskDraft, TaskView
from officeflow.domain.enums import ReminderDeliveryStatus, ReminderRelation, TaskStatus
from officeflow.domain.reminder import ReminderRuleInput
from officeflow.presentation.record_dialog import TaskRecordsDialog
from officeflow.presentation.task_editor import TaskEditorDialog
from tests.ui.test_multiple_reminders_ui import live_window  # noqa: F401


def open_alert(window, task, now):
    alerts = window._reminder_service.poll_due(now=now)
    alert = next(item for item in alerts if item.task.id == task.id)
    window._present_reminder_alerts((alert,))
    return alert, window._reminder_dialog


def test_view_keeps_alert_unhandled_main_selection_and_reuses_record(live_window, qtbot):  # noqa: F811
    window, env, task, now, repo = live_window
    window._selected_task_id = task.id
    window._open_selected_records(initial_tab="checklist")
    record = next(item for item in window._file_dialogs if isinstance(item, TaskRecordsDialog))
    record.result_edit.setPlainText("작성 중인 내용")
    window._search.setText("검색 상태 유지")
    selection = window._selected_task_id, window._selected_occurrence_start
    alert, popup = open_alert(window, task, now)
    before = repo.get_reminder_delivery(alert.delivery.id)
    qtbot.mouseClick(popup.view_task_button, Qt.MouseButton.LeftButton)
    assert popup.isVisible() and popup.alert_list.count() == 1
    assert repo.get_reminder_delivery(alert.delivery.id) == before
    assert env.tasks.get(task.id).status is TaskStatus.ACTIVE
    assert window._search.text() == "검색 상태 유지"
    assert (window._selected_task_id, window._selected_occurrence_start) == selection
    assert [item for item in window._file_dialogs if isinstance(item, TaskRecordsDialog)] == [
        record
    ]
    assert record.result_edit.toPlainText() == "작성 중인 내용"
    assert QApplication.activeModalWidget() is None
    assert not record.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    qtbot.mouseClick(popup.acknowledge_button, Qt.MouseButton.LeftButton)
    assert (
        repo.get_reminder_delivery(alert.delivery.id).status is ReminderDeliveryStatus.ACKNOWLEDGED
    )
    assert record.isVisible()


def test_view_selects_real_repeating_occurrence_and_distinct_windows(live_window, qtbot):  # noqa: F811
    window, _env, _task, now, repo = live_window
    task = window._task_save_service.save(
        TaskDraft("회차 보기", starts_at=now, recurrence_rule="FREQ=DAILY;COUNT=3"),
        edits=(ReminderEdit(ReminderRuleInput(ReminderRelation.START, 0)),),
        now=now,
    )
    alert, popup = open_alert(window, task, now)
    qtbot.mouseClick(popup.view_task_button, Qt.MouseButton.LeftButton)
    first = next(item for item in window._file_dialogs if isinstance(item, TaskRecordsDialog))
    assert first._task_id == task.id
    assert first._occurrence_start == alert.occurrence_start == now
    second_alerts = window._reminder_service.poll_due(now=now + timedelta(days=1))
    next_alert = next(
        item for item in second_alerts if item.task.id == task.id and item.occurrence_start != now
    )
    window._present_reminder_alerts((next_alert,))
    popup.alert_list.setCurrentRow(popup.alert_list.count() - 1)
    qtbot.mouseClick(popup.view_task_button, Qt.MouseButton.LeftButton)
    records = [item for item in window._file_dialogs if isinstance(item, TaskRecordsDialog)]
    assert len(records) == 2
    assert {item._occurrence_start for item in records} == {now, now + timedelta(days=1)}
    assert repo.get_reminder_delivery(alert.delivery.id).status is ReminderDeliveryStatus.FIRED


def test_stale_or_deleted_task_does_not_open_wrong_record(live_window, qtbot):  # noqa: F811
    window, env, task, now, repo = live_window
    alert, popup = open_alert(window, task, now)
    env.tasks.move_to_trash(task.id)
    before = repo.get_reminder_delivery(alert.delivery.id)
    qtbot.mouseClick(popup.view_task_button, Qt.MouseButton.LeftButton)
    assert not any(isinstance(item, TaskRecordsDialog) for item in window._file_dialogs)
    assert repo.get_reminder_delivery(alert.delivery.id) == before


def test_record_from_worklog_owner_is_reused(live_window, qtbot):  # noqa: F811
    window, _env, task, now, _repo = live_window
    window._open_work_logs()
    owner = window._file_dialogs[-1]
    child = TaskRecordsDialog(
        task, task_service=window._task_service, record_service=window._record_service, parent=owner
    )
    qtbot.addWidget(child)
    owner._record_dialogs.append(child)
    child.show()
    child.result_edit.setPlainText("기존 창 초안")
    _alert, popup = open_alert(window, task, now)
    qtbot.mouseClick(popup.view_task_button, Qt.MouseButton.LeftButton)
    assert child.result_edit.toPlainText() == "기존 창 초안"
    assert not any(isinstance(item, TaskRecordsDialog) for item in window._file_dialogs)
    owner._record_dialogs.remove(child)
    child.shutdown()


@pytest.mark.parametrize("scheduled", [False, True])
def test_real_new_task_save_persists_matching_alarm_rules(live_window, qtbot, scheduled):  # noqa: F811
    window, env, _task, _now, repo = live_window
    window._open_new_task()
    editor = next(item for item in window._file_dialogs if isinstance(item, TaskEditorDialog))
    editor.title_edit.setText("새 입력 실제 저장")
    if not scheduled:
        editor.schedule_combo.setCurrentIndex(editor.schedule_combo.findData("none"))
    else:
        editor.start_time_edit.setText("1124")
    qtbot.mouseClick(editor.save_button, Qt.MouseButton.LeftButton)
    assert not editor.error_label.isVisible(), editor.error_label.text()
    task = env.tasks.list(TaskView.ALL, search="새 입력 실제 저장")[0]
    assert task.title == "새 입력 실제 저장"
    rules = repo.list_reminders(task.id)
    if scheduled:
        assert task.starts_at is not None
        assert len(rules) == 1 and rules[0].offset_minutes == 0
        assert task.starts_at.astimezone(ZoneInfo("Asia/Seoul")).strftime("%H:%M") == "11:24"
    else:
        assert task.starts_at is None and rules == ()
