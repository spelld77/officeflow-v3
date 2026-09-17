from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, Qt
from pytestqt.qtbot import QtBot

from officeflow.domain.enums import ReminderRelation
from officeflow.presentation.task_editor import TaskEditorDialog
from officeflow.presentation.theme import LIGHT_STYLESHEET


def test_editor_builds_inclusive_multiday_all_day_schedule(qtbot: QtBot) -> None:
    editor = TaskEditorDialog(timezone="Asia/Seoul")
    qtbot.addWidget(editor)
    editor.title_edit.setText("출장")
    editor.schedule_combo.setCurrentIndex(editor.schedule_combo.findData("range"))
    editor.all_day_check.setChecked(True)
    editor.start_date_edit.setDate(QDate(2026, 9, 14))
    editor.end_date_edit.setDate(QDate(2026, 9, 17))

    editor._validate_and_accept()
    draft = editor.draft()

    assert draft.starts_at is not None
    assert draft.ends_at is not None
    assert draft.starts_at.astimezone(ZoneInfo("Asia/Seoul")).date() == date(2026, 9, 14)
    assert (draft.ends_at - draft.starts_at).days == 4


def test_editor_uses_calendar_date_for_new_schedule(qtbot: QtBot) -> None:
    editor = TaskEditorDialog(timezone="Asia/Seoul", initial_date=date(2026, 10, 3))
    qtbot.addWidget(editor)

    assert editor.start_date_edit.date() == QDate(2026, 10, 3)
    assert editor.end_date_edit.date() == QDate(2026, 10, 3)


def test_editor_builds_daily_recurrence_with_until_date(qtbot: QtBot) -> None:
    editor = TaskEditorDialog(timezone="Asia/Seoul", initial_date=date(2026, 9, 14))
    qtbot.addWidget(editor)
    editor.title_edit.setText("매일 점검")
    editor.repeat_combo.setCurrentIndex(editor.repeat_combo.findData("DAILY"))
    editor.repeat_interval.setValue(2)
    editor.repeat_until_check.setChecked(True)
    editor.repeat_until_date.setDate(QDate(2026, 9, 20))

    editor._validate_and_accept()

    assert editor.draft().recurrence_rule == ("FREQ=DAILY;INTERVAL=2;UNTIL=20260920T145959Z")


def test_tab_moves_through_title_and_multiline_description(qtbot: QtBot) -> None:
    editor = TaskEditorDialog(timezone="Asia/Seoul")
    qtbot.addWidget(editor)
    editor.show()
    editor.title_edit.setFocus()

    qtbot.keyPress(editor.title_edit, Qt.Key.Key_Tab)
    assert editor.focusWidget() is editor.description_edit

    editor.description_edit.setPlainText("상세 내용")
    qtbot.keyPress(editor.description_edit, Qt.Key.Key_Tab)
    assert editor.focusWidget() is editor.priority_combo
    assert editor.description_edit.toPlainText() == "상세 내용"


def test_tab_skips_hidden_schedule_fields_and_reaches_repeat(qtbot: QtBot) -> None:
    editor = TaskEditorDialog(timezone="Asia/Seoul")
    qtbot.addWidget(editor)
    editor.show()
    editor.start_date_edit.setFocus()

    qtbot.keyPress(editor.start_date_edit, Qt.Key.Key_Tab)

    assert editor.focusWidget() is editor.repeat_combo


def test_editor_builds_start_and_end_reminder_rules(qtbot: QtBot) -> None:
    editor = TaskEditorDialog(timezone="Asia/Seoul", initial_date=date(2026, 9, 14))
    qtbot.addWidget(editor)
    editor.title_edit.setText("알림 업무")
    editor.start_reminder_combo.setCurrentIndex(editor.start_reminder_combo.findData(-10))
    editor.end_reminder_combo.setCurrentIndex(editor.end_reminder_combo.findData(-30))

    editor._validate_and_accept()

    assert [(rule.relation, rule.offset_minutes) for rule in editor.draft().reminder_rules] == [
        (ReminderRelation.START, -10),
        (ReminderRelation.END, -30),
    ]


def test_timed_schedule_inputs_keep_their_full_height(qtbot: QtBot) -> None:
    editor = TaskEditorDialog(timezone="Asia/Seoul")
    editor.setStyleSheet(LIGHT_STYLESHEET)
    qtbot.addWidget(editor)
    editor.all_day_check.setChecked(False)
    editor.show()

    assert editor.start_time_edit.height() >= editor.start_time_edit.sizeHint().height()
    assert editor.end_time_edit.height() >= editor.end_time_edit.sizeHint().height()
