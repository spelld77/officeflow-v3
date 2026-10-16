from datetime import UTC, datetime

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QMessageBox

from officeflow.application.tasks import TaskDraft
from officeflow.domain.task import Task
from officeflow.presentation.task_editor import TaskEditorDialog
from officeflow.presentation.theme import LIGHT_STYLESHEET


def test_editor_year_change_requires_confirmation_and_keeps_draft(qtbot):
    saved: list[TaskDraft] = []
    task = Task.create(title="연도 확인", starts_at=datetime(2027, 10, 15, tzinfo=UTC))
    editor = TaskEditorDialog(
        timezone="Asia/Seoul", task=task, on_save=lambda e: saved.append(e.draft())
    )
    qtbot.addWidget(editor)
    editor.setStyleSheet(LIGHT_STYLESHEET)
    editor.show()
    editor.start_date_edit.setDate(QDate(2028, 10, 15))
    editor._validate_and_accept()
    assert not saved
    prompt = editor.findChild(QMessageBox)
    assert prompt is not None and prompt.isVisible() and not prompt.isModal()
    qtbot.wait(80)  # Let deferred Qt form/scroll layouts settle before visual evidence.
    assert editor.start_date_caution.notice.isVisible()
    assert editor.grab().save("artifacts/date-review-editor.png")
    assert prompt.grab().save("artifacts/date-review-confirmation.png")
    prompt.button(QMessageBox.StandardButton.No).click()
    assert editor.isVisible() and not saved
    editor._validate_and_accept()
    prompt = editor._date_review.dialog
    prompt.button(QMessageBox.StandardButton.Yes).click()
    assert len(saved) == 1 and not editor.isVisible()
    assert saved[0].starts_at.astimezone().year == 2028


def test_confirmation_does_not_save_newly_edited_input(qtbot):
    saved = []
    task = Task.create(title="확인 중 변경", starts_at=datetime(2027, 10, 15, tzinfo=UTC))
    editor = TaskEditorDialog(
        timezone="Asia/Seoul", task=task, on_save=lambda e: saved.append(e.draft())
    )
    qtbot.addWidget(editor)
    editor.show()
    editor.start_date_edit.setDate(QDate(2028, 10, 15))
    editor._validate_and_accept()
    prompt = editor._date_review.dialog
    editor.start_date_edit.setDate(QDate(2029, 10, 15))
    qtbot.mouseClick(prompt.button(QMessageBox.StandardButton.Yes), Qt.MouseButton.LeftButton)
    assert not saved and editor.isVisible()
    assert editor._date_review.dialog is not None


def test_approved_year_change_save_failure_preserves_input_and_approval(qtbot):
    attempts = []

    def save(editor):
        attempts.append(editor.draft())
        if len(attempts) == 1:
            raise OSError("DB 일시 실패")

    task = Task.create(title="재시도", starts_at=datetime(2027, 10, 15, tzinfo=UTC))
    editor = TaskEditorDialog(timezone="Asia/Seoul", task=task, on_save=save)
    qtbot.addWidget(editor)
    editor.show()
    editor.start_date_edit.setDate(QDate(2028, 10, 15))
    editor._validate_and_accept()
    editor._date_review.dialog.button(QMessageBox.StandardButton.Yes).click()
    assert len(attempts) == 1 and editor.isVisible()
    assert "DB 일시 실패" in editor.error_label.text()
    assert editor._date_review.dialog is None
    editor._validate_and_accept()
    assert len(attempts) == 2 and not editor.isVisible()


def test_nested_alarm_year_review_is_deferred_until_task_save(qtbot):
    from officeflow.domain.enums import ReminderRelation
    from officeflow.domain.reminder import ReminderRuleInput

    at = datetime(2027, 10, 15, tzinfo=UTC)
    task = Task.create(title="알림 연도", starts_at=at)
    saved = []
    editor = TaskEditorDialog(
        timezone="UTC",
        task=task,
        reminder_rules=(ReminderRuleInput(ReminderRelation.ABSOLUTE, absolute_at=at),),
        on_save=lambda e: saved.append(e.draft()),
    )
    qtbot.addWidget(editor)
    editor.show()
    editor._open_reminder_settings()
    settings = editor._reminder_settings
    settings.list.setCurrentRow(0)
    settings._open_rule(True)
    settings._child.date_edit.setDate(QDate(2028, 10, 15))
    settings._child._accept_rule()
    settings._apply()
    assert editor._reminder_settings is None and not saved
    editor._validate_and_accept()
    assert editor._date_review.dialog is not None and not saved
    editor._date_review.dialog.button(QMessageBox.StandardButton.Yes).click()
    assert len(saved) == 1
