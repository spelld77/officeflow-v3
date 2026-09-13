from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, Qt
from pytestqt.qtbot import QtBot

from officeflow.presentation.task_editor import TaskEditorDialog


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


def test_tab_skips_hidden_schedule_fields(qtbot: QtBot) -> None:
    editor = TaskEditorDialog(timezone="Asia/Seoul")
    qtbot.addWidget(editor)
    editor.show()
    editor.start_date_edit.setFocus()

    qtbot.keyPress(editor.start_date_edit, Qt.Key.Key_Tab)

    assert editor.focusWidget() is editor.save_button
