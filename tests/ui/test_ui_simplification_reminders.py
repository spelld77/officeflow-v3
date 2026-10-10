"""A concise actual summary never presents an unsaved prediction as a reservation."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from officeflow.application.reminder_runtime import ReminderRuntimeState
from officeflow.domain.enums import ReminderRelation, TaskStatus
from officeflow.domain.reminder import Reminder
from officeflow.domain.task import Task
from officeflow.presentation.task_editor import TaskEditorDialog


def editor_with_runtime(qtbot):
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    task = replace(Task.create(title="예약", starts_at=now + timedelta(days=20)), id=1)
    rule = Reminder(7, 1, ReminderRelation.START, 0, None, True)
    state = ReminderRuntimeState(task, (), (), (rule,), now)
    editor = TaskEditorDialog(timezone="Asia/Seoul", task=task, reminders=(rule,))
    qtbot.addWidget(editor)
    editor.update_runtime_state(state)
    return editor, state


def test_unchanged_summary_has_one_actual_line_and_explicit_settings(qtbot):
    editor, _state = editor_with_runtime(qtbot)
    assert editor.reminder_summary.text().count("\n") == 0
    assert editor.reminder_summary.text().startswith("알림 1개 · 다음 ")
    assert editor.reminder_settings_button.text() == "알림 설정…"
    editor.title_edit.setText("제목만 수정")
    editor._refresh_reminder_summary()
    assert "저장 전 변경" not in editor.reminder_summary.text()
    editor.reject()


def test_schedule_draft_changes_do_not_replace_actual_reservation(qtbot):
    editor, _state = editor_with_runtime(qtbot)
    actual = editor.reminder_summary.text()
    editor.start_date_edit.setDate(editor.start_date_edit.date().addDays(1))
    assert editor.reminder_summary.text().splitlines()[0] == actual
    assert "저장 전 변경 있음" in editor.reminder_summary.text()
    assert "저장 전 예상" in editor.reminder_summary.toolTip()
    editor.reject()


def test_external_completion_is_not_labelled_as_user_draft(qtbot):
    editor, state = editor_with_runtime(qtbot)
    date = editor.start_date_edit.date()
    completed = replace(state, task=replace(state.task, status=TaskStatus.COMPLETED))
    editor.update_runtime_state(completed)
    assert "업무 완료 · 이후 알림 중단" in editor.reminder_summary.text()
    assert "저장 전 변경" not in editor.reminder_summary.text()
    assert editor.start_date_edit.date() == date
    editor.reject()


def test_new_task_prediction_says_not_saved(qtbot):
    editor = TaskEditorDialog(timezone="Asia/Seoul")
    qtbot.addWidget(editor)
    assert editor.reminder_summary.text().startswith("저장 전 · 알림 1개 · 예상 ")
    editor.reject()
