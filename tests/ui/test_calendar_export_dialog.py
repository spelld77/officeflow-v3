from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate
from pytestqt.qtbot import QtBot

from officeflow.application.exporting import CalendarExportScope
from officeflow.application.tasks import TaskQuery
from officeflow.presentation.calendar_export_dialog import CalendarExportDialog


def test_calendar_export_dialog_uses_safe_defaults(qtbot: QtBot) -> None:
    dialog = CalendarExportDialog(query=TaskQuery())
    qtbot.addWidget(dialog)

    options = dialog.options()

    assert options.scope is CalendarExportScope.CURRENT_LIST
    assert not options.include_recurring
    assert options.include_completed
    assert not dialog.selected_tasks_radio.isEnabled()
    assert not dialog.range_widget.isEnabled()


def test_calendar_export_dialog_builds_selected_and_date_range_options(
    qtbot: QtBot,
) -> None:
    query = TaskQuery(search="출장")
    dialog = CalendarExportDialog(query=query, selected_task_ids=(7,))
    qtbot.addWidget(dialog)

    dialog.selected_tasks_radio.setChecked(True)
    selected = dialog.options()
    assert selected.scope is CalendarExportScope.SELECTED_TASKS
    assert selected.selected_task_ids == (7,)

    dialog.date_range_radio.setChecked(True)
    dialog.date_from_edit.setDate(QDate(2026, 9, 15))
    dialog.date_to_edit.setDate(QDate(2026, 9, 20))
    dialog.include_recurring_check.setChecked(True)
    ranged = dialog.options()

    assert dialog.range_widget.isEnabled()
    assert ranged.scope is CalendarExportScope.DATE_RANGE
    assert ranged.date_from == date(2026, 9, 15)
    assert ranged.date_to == date(2026, 9, 20)
    assert ranged.include_recurring
