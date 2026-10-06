from datetime import UTC, datetime

import pytest
from PySide6.QtCore import QDate, QPoint, Qt
from PySide6.QtWidgets import QDateEdit, QDateTimeEdit, QLabel

from officeflow.application.tasks import TaskDraft, TaskQuery
from officeflow.domain.task import Task
from officeflow.presentation.attachment_search_page import AttachmentSearchPageWidget
from officeflow.presentation.calendar_export_dialog import CalendarExportDialog
from officeflow.presentation.dates import date_with_weekday
from officeflow.presentation.record_dialog import TaskRecordsDialog, WorkLogBrowserDialog
from officeflow.presentation.reminder_settings import ReminderRuleDialog
from officeflow.presentation.task_editor import TaskEditorDialog
from officeflow.presentation.theme import LIGHT_STYLESHEET
from tests.integration.test_attachment_search import search_environment


def test_weekday_is_inside_input_and_has_no_editable_section(qtbot):
    edit = QDateEdit(QDate(2026, 10, 6))
    qtbot.addWidget(edit)
    assert date_with_weekday(edit) is edit
    edit.show()
    assert edit.text() == "2026-10-06 (화)"
    assert edit.sectionCount() == 3
    assert not edit.findChildren(QLabel)
    edit.setDate(QDate(2026, 10, 7))
    assert edit.text() == "2026-10-07 (수)"
    assert edit.date().toPython().isoformat() == "2026-10-07"


def test_day_arrows_and_typing_keep_current_section_and_date(qtbot):
    edit = date_with_weekday(QDateEdit(QDate(2026, 10, 6)))
    qtbot.addWidget(edit)
    edit.show()
    edit.setFocus()
    edit.setCurrentSection(QDateTimeEdit.Section.DaySection)
    qtbot.keyPress(edit, Qt.Key.Key_Up)
    assert edit.date() == QDate(2026, 10, 7)
    assert edit.currentSection() is QDateTimeEdit.Section.DaySection
    assert edit.text().endswith("(수)")
    edit.setSelectedSection(QDateTimeEdit.Section.DaySection)
    qtbot.keyClicks(edit, "12")
    qtbot.keyPress(edit, Qt.Key.Key_Return)
    assert edit.date() == QDate(2026, 10, 12)
    assert edit.text().endswith("(월)")
    edit.setSelectedSection(QDateTimeEdit.Section.YearSection)
    qtbot.keyClicks(edit, "2028")
    qtbot.keyPress(edit, Qt.Key.Key_Return)
    assert edit.date() == QDate(2028, 10, 12)
    assert edit.text().endswith("(목)")


def test_calendar_popup_selection_refreshes_weekday(qtbot):
    edit = date_with_weekday(QDateEdit(QDate(2026, 10, 6)))
    edit.setCalendarPopup(True)
    edit.setStyleSheet(LIGHT_STYLESHEET)
    qtbot.addWidget(edit)
    edit.resize(240, edit.sizeHint().height())
    edit.show()
    qtbot.mouseClick(
        edit, Qt.MouseButton.LeftButton, pos=QPoint(edit.width() - 8, edit.height() // 2)
    )
    calendar = edit.calendarWidget()
    qtbot.waitUntil(calendar.isVisible, timeout=1500)
    calendar.setSelectedDate(QDate(2026, 10, 8))
    qtbot.keyPress(calendar, Qt.Key.Key_Return)
    assert edit.date() == QDate(2026, 10, 8)
    assert edit.text() == "2026-10-08 (목)"


@pytest.mark.parametrize("advanced", [False, True])
def test_editor_and_reminder_date_fields_contain_weekday(qtbot, tmp_path, advanced):
    task = Task.create(title="날짜 표시", starts_at=datetime(2026, 10, 6, 1, 0, tzinfo=UTC))
    editor = TaskEditorDialog(timezone="Asia/Seoul", task=task)
    editor.setStyleSheet(LIGHT_STYLESHEET)
    qtbot.addWidget(editor)
    editor.advanced_schedule_check.setChecked(advanced)
    editor.repeat_combo.setCurrentIndex(editor.repeat_combo.findData("DAILY"))
    editor.repeat_until_check.setChecked(advanced)
    editor.show()
    qtbot.wait(60)
    for field in (editor.start_date_edit, editor.end_date_edit, editor.repeat_until_date):
        assert editor._date_rows[field] is field
        assert field.sectionCount() == 3
        assert "(" in field.text()
    assert editor.grab().save(str(tmp_path / "weekday-inside-editor.png"))
    dialog = ReminderRuleDialog(task, parent=editor)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(50)
    assert dialog.date_row is dialog.date_edit
    assert dialog.date_edit.sectionCount() == 3
    assert dialog.grab().save(str(tmp_path / "weekday-inside-reminder.png"))


@pytest.mark.parametrize("screen", ["export", "record", "log_search", "attachment_search"])
def test_other_date_input_screens_keep_weekday_inside_field(qtbot, tmp_path, screen):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft("날짜 화면 확인"))
        if screen == "export":
            widget = CalendarExportDialog(query=TaskQuery())
            widget.date_range_radio.setChecked(True)
            fields = (widget.date_from_edit, widget.date_to_edit)
        elif screen == "record":
            widget = TaskRecordsDialog(task, task_service=env.tasks, record_service=env.records)
            widget.select_tab("work_log")
            fields = (widget.log_date_edit,)
        elif screen == "log_search":
            widget = WorkLogBrowserDialog(task_service=env.tasks, record_service=env.records)
            widget.search_edit.setText("보고서")
            fields = (widget.date_edit, widget.range_from_edit, widget.range_to_edit)
        else:
            widget = AttachmentSearchPageWidget(env.search, timezone="Asia/Seoul")
            widget.more_conditions.setChecked(True)
            widget.range_check.setChecked(True)
            widget.resize(660, 500)
            fields = (widget.date_from, widget.date_to)
        widget.setStyleSheet(LIGHT_STYLESHEET)
        qtbot.addWidget(widget)
        try:
            widget.show()
            for field in fields:
                field.setDate(QDate(2028, 2, 29))
                assert field.text() == "2028-02-29 (화)"
                assert field.sectionCount() == 3
            qtbot.wait(100)
            for field in fields:
                if field.isVisible():
                    assert (
                        field.lineEdit().contentsRect().width()
                        >= field.fontMetrics().horizontalAdvance(field.text())
                    )
            assert not widget.findChildren(QLabel, "dateWeekday")
            assert widget.grab().save(str(tmp_path / f"weekday-inside-{screen}.png"))
            if screen == "record":
                assert widget.log_date_edit.isVisible()
            elif screen == "log_search":
                widget.search_edit.clear()
                widget._refresh()
                assert widget.date_edit.isVisible()
                assert (
                    widget.date_edit.lineEdit().contentsRect().width()
                    >= widget.date_edit.fontMetrics().horizontalAdvance(widget.date_edit.text())
                )
                assert widget.grab().save(str(tmp_path / "weekday-inside-log-day.png"))
        finally:
            if hasattr(widget, "shutdown"):
                widget.shutdown()
