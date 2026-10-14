from datetime import UTC, datetime

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtWidgets import QWidget

from officeflow.application.tasks import TaskDraft
from officeflow.presentation.record_dialog import TaskRecordsDialog
from tests.integration.test_attachment_search import search_environment
from tests.ui.test_combined_task_view import make_window


class WindowShows(QObject):
    def __init__(self):
        super().__init__()
        self.windows = []

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show and isinstance(watched, QWidget) and watched.isWindow():
            self.windows.append((type(watched).__name__, watched.objectName()))
        return False


@pytest.mark.parametrize("kind", ["description", "recurring", "trash", "attachments", "empty"])
def test_task_view_does_not_show_temporary_top_level_widgets(qtbot, qapp, tmp_path, kind):
    with search_environment(tmp_path) as env:
        start = datetime(2026, 10, 15, 9, tzinfo=UTC)
        task = env.tasks.create(
            TaskDraft(
                "No temporary window",
                description="Saved description" if kind in {"description", "attachments"} else "",
                starts_at=start,
                recurrence_rule="FREQ=DAILY" if kind == "recurring" else None,
            )
        )
        if kind == "trash":
            task = env.tasks.move_to_trash(task.id)
        seen = WindowShows()
        qapp.installEventFilter(seen)
        records = None
        try:
            records = TaskRecordsDialog(
                task,
                task_service=env.tasks,
                record_service=env.records,
                attachment_service=env.attachments,
                occurrence_start=start if kind == "recurring" else None,
                initial_tab="attachments" if kind == "attachments" else None,
            )
            qtbot.addWidget(records)
            assert seen.windows == []
            records.show()
            qtbot.wait(20)
            assert seen.windows == [("TaskRecordsDialog", "")]
            records.refresh_header()
            records.show_entry()
            qtbot.wait(20)
            assert seen.windows == [("TaskRecordsDialog", "")]
        finally:
            qapp.removeEventFilter(seen)
            if records is not None:
                records.shutdown()


@pytest.mark.parametrize("entry", ["list", "calendar"])
def test_doubleclick_opens_only_task_view_window(qtbot, qapp, tmp_path, entry):
    with search_environment(tmp_path) as env:
        start = datetime(2026, 10, 15, 9, tzinfo=UTC)
        task = env.tasks.create(TaskDraft("Double click", description="Read me", starts_at=start))
        window = make_window(qtbot, env, view_clock=lambda: start)
        seen = WindowShows()
        try:
            qtbot.wait(20)
            if entry == "list":
                listing = window._task_list
                listing.doItemsLayout()
                rect = listing.visualRect(window._task_model.index_for_task(task.id))
            else:
                window._show_calendar()
                window._calendar_page._set_month_and_date(start.date())
                listing = window._calendar_page.day_list
                qtbot.waitUntil(lambda: listing.count() == 1)
                rect = listing.visualItemRect(listing.item(0))
            qapp.installEventFilter(seen)
            position = rect.topLeft() + QPoint(90, rect.height() // 2)
            # QTest's DClick sends the second press, not the preceding ordinary click.
            qtbot.mouseClick(listing.viewport(), Qt.MouseButton.LeftButton, pos=position)
            qtbot.mouseDClick(
                listing.viewport(),
                Qt.MouseButton.LeftButton,
                pos=position,
            )
            qtbot.waitUntil(
                lambda: any(isinstance(d, TaskRecordsDialog) for d in window._file_dialogs)
            )
            assert seen.windows == [("TaskRecordsDialog", "")]
        finally:
            qapp.removeEventFilter(seen)
            window.shutdown()
