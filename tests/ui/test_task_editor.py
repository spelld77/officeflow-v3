from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate
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
