from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QHelpEvent, QMouseEvent
from PySide6.QtWidgets import QFileDialog, QStyleOptionViewItem

from officeflow.application.tasks import TaskDraft, TaskView
from officeflow.domain.enums import OccurrenceStatus, TaskStatus
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.attachment_import import import_start_directory
from officeflow.presentation.main_window import MainWindow
from officeflow.presentation.record_dialog import TaskRecordsDialog, WorkLogBrowserDialog
from officeflow.presentation.task_editor import TaskEditorDialog
from tests.integration.test_attachment_search import search_environment


def make_window(qtbot, env, **kwargs):
    window = MainWindow(
        AppSettings(automatic_backup_enabled=False),
        env.tasks,
        record_service=env.records,
        attachment_service=env.attachments,
        hourly_notifications_enabled=False,
        **kwargs,
    )
    qtbot.addWidget(window)
    window.show()
    window._set_view(TaskView.ALL)
    return window


def open_view(window, task_id):
    index = window._task_model.index_for_task(task_id)
    window._task_list.setCurrentIndex(index)
    window._open_task_from_index(index, from_keyboard=True)
    return next(item for item in window._file_dialogs if isinstance(item, TaskRecordsDialog))


def test_list_view_shows_description_and_explicit_edit_only(qtbot, tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft("Read first", description="설명 <b>literal</b>"))
        window = make_window(qtbot, env)
        try:
            window._last_record_tab = "attachments"
            records = open_view(window, task.id)
            assert records.current_tab_key == "checklist"
            assert records.description_panel.body.toPlainText() == task.description
            assert not records.description_panel.collapsed
            assert not any(isinstance(d, TaskEditorDialog) for d in window._file_dialogs)
            records.edit_task_button.click()
            assert sum(isinstance(d, TaskEditorDialog) for d in window._file_dialogs) == 1
            records.edit_task_button.click()
            assert sum(isinstance(d, TaskEditorDialog) for d in window._file_dialogs) == 1
        finally:
            window.shutdown()


def test_reused_view_preserves_drafts_and_latest_header(qtbot, tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft("Old", description="Old description"))
        window = make_window(qtbot, env)
        try:
            records = open_view(window, task.id)
            records.select_tab("result")
            records.result_edit.setPlainText("unsaved result")
            records.log_content_edit.setPlainText("unsaved log")
            env.tasks.update(task.id, TaskDraft("New", description="New description"))
            window._refresh_tasks()
            window._open_selected_records()
            assert sum(isinstance(d, TaskRecordsDialog) for d in window._file_dialogs) == 1
            assert records.current_tab_key == "result"
            assert records.description_panel.body.toPlainText() == "New description"
            assert records.result_edit.toPlainText() == "unsaved result"
            assert records.log_content_edit.toPlainText() == "unsaved log"
            assert "New" in records.windowTitle()
        finally:
            window.shutdown()


def test_activation_does_not_reenter_saved_header_database_query(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft("activation", description="saved description"))
        records = TaskRecordsDialog(task, task_service=env.tasks, record_service=env.records)
        qtbot.addWidget(records)
        called = []
        monkeypatch.setattr(records, "refresh_header", lambda: called.append(True))
        records.show()
        QApplication.sendEvent(records, QEvent(QEvent.Type.ActivationChange))
        assert called == []
        records.shutdown()


def test_trash_doubleclick_is_read_only_view_not_restore(qtbot, tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft("trash", description="still readable"))
        env.tasks.move_to_trash(task.id)
        window = make_window(qtbot, env)
        try:
            window._set_view(TaskView.TRASH)
            records = open_view(window, task.id)
            assert records._read_only and not records.edit_task_button.isEnabled()
            assert records.result_edit.isReadOnly()
            assert records.description_panel.body.toPlainText() == "still readable"
            assert env.tasks.get_including_deleted(task.id).deleted_at is not None
        finally:
            window.shutdown()


@pytest.mark.parametrize("compact", [False, True])
def test_real_badge_mouse_click_and_doubleclick_do_not_edit(qtbot, tmp_path, compact):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft("Attachment", description="Read separately"))
        env.add(task.id, "file.txt")
        window = make_window(qtbot, env)
        try:
            window.resize(900, 650)
            window._task_delegate.set_compact(compact)
            window._task_list.doItemsLayout()
            index = window._task_model.index_for_task(task.id)
            option = QStyleOptionViewItem()
            option.rect = window._task_list.visualRect(index)
            point = window._task_delegate.attachment_rect(option).center().toPoint()
            qtbot.mouseClick(window._task_list.viewport(), Qt.MouseButton.LeftButton, pos=point)
            qtbot.waitUntil(
                lambda: any(isinstance(d, TaskRecordsDialog) for d in window._file_dialogs)
            )
            records = next(d for d in window._file_dialogs if isinstance(d, TaskRecordsDialog))
            assert records.current_tab_key == "attachments" and records.description_panel.collapsed
            qtbot.mouseDClick(window._task_list.viewport(), Qt.MouseButton.LeftButton, pos=point)
            assert sum(isinstance(d, TaskRecordsDialog) for d in window._file_dialogs) == 1
            assert not any(isinstance(d, TaskEditorDialog) for d in window._file_dialogs)
            assert records.current_tab_key == "attachments" and records.description_panel.collapsed
            # Enter is independent of the short-lived mouse double-click suppression.
            window._task_list.setCurrentIndex(index)
            window._activate_selected_task()
            assert not records.description_panel.collapsed
        finally:
            window.shutdown()


def test_press_badge_release_outside_does_not_open_attachment(qtbot, tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft("movement"))
        env.add(task.id, "file.txt")
        window = make_window(qtbot, env)
        try:
            index = window._task_model.index_for_task(task.id)
            option = QStyleOptionViewItem()
            option.rect = window._task_list.visualRect(index)
            badge = window._task_delegate.attachment_rect(option).center()
            outside = QPointF(option.rect.left() + 80, option.rect.center().y())
            for kind, point in (
                (QEvent.Type.MouseButtonPress, badge),
                (QEvent.Type.MouseButtonRelease, outside),
            ):
                event = QMouseEvent(
                    kind,
                    point,
                    point,
                    Qt.MouseButton.LeftButton,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
                window._task_delegate.editorEvent(event, window._task_model, option, index)
            assert not any(isinstance(d, TaskRecordsDialog) for d in window._file_dialogs)
        finally:
            window.shutdown()


def test_moved_occurrence_view_is_readonly_projection_and_editor_uses_template(
    qtbot, tmp_path, monkeypatch
):
    with search_environment(tmp_path) as env:
        start = datetime(2026, 10, 14, 3, tzinfo=UTC)
        task = env.tasks.create(
            TaskDraft(
                "series",
                starts_at=start,
                ends_at=start + timedelta(hours=1),
                recurrence_rule="FREQ=DAILY",
            )
        )
        original = start + timedelta(days=1)
        occurrence = env.tasks.ensure_occurrence(task.id, original)
        env.tasks._repository.save_occurrence(
            replace(
                occurrence,
                effective_start=original + timedelta(days=5),
                effective_end=original + timedelta(days=5, hours=1),
            )
        )
        window = make_window(qtbot, env)
        try:
            records = window._show_task_records(task, occurrence_start=original)
            assert records._occurrence_start == original
            assert records._display_task.starts_at == original + timedelta(days=5)
            opened = []
            monkeypatch.setattr(window, "_open_editor", lambda task, **_kwargs: opened.append(task))
            records.edit_task_button.click()
            assert opened[0].starts_at == start
            records.result_edit.setPlainText("unsaved")
            env.tasks.transition_occurrence(task.id, original, OccurrenceStatus.COMPLETED)
            window._refresh_tasks()
            assert records._display_task.status is TaskStatus.COMPLETED
            assert records.result_edit.toPlainText() == "unsaved"
            pending = env.tasks.view_task(task, start + timedelta(days=2))
            assert pending.occurrence_start == start + timedelta(days=2)
            assert env.tasks.occurrence(task.id, pending.occurrence_start) is None
        finally:
            window.shutdown()


def test_picker_remembers_selection_not_import_success_or_cancellation(
    qtbot, tmp_path, monkeypatch
):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft("folders"))
        window = make_window(qtbot, env)
        try:
            first = open_view(window, task.id)
            first.select_tab("attachments")
            paths = (str(tmp_path / "picked" / "a.txt"), str(tmp_path / "picked" / "b.txt"))
            submitted = []
            monkeypatch.setattr(
                QFileDialog, "getOpenFileNames", lambda *_args, **_kwargs: (list(paths), "")
            )
            monkeypatch.setattr(
                first, "_add_attachment_sources", lambda sources: submitted.append(sources)
            )
            first._choose_attachment()
            assert window._settings.attachment_import_directory == str(tmp_path / "picked")
            assert len(submitted[0]) == 2
            monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *_args, **_kwargs: ([], ""))
            first._choose_attachment()
            assert len(submitted) == 1
            window._open_work_logs()
            browser = next(d for d in window._file_dialogs if isinstance(d, WorkLogBrowserDialog))
            assert (
                browser._attachment_import_directory == window._settings.attachment_import_directory
            )
            browser._selected_task_context = task.id, None
            browser._open_selected_records()
            child = browser._record_dialogs[0]
            child.importDirectoryChanged.emit(str(tmp_path))
            assert window._settings.attachment_import_directory == str(tmp_path)
            assert first._attachment_import_directory == str(tmp_path)
        finally:
            window.shutdown()


def test_folder_setting_save_failure_does_not_block_picker(qtbot, tmp_path):
    with search_environment(tmp_path) as env:

        def fail(_settings):
            raise OSError("synthetic settings failure")

        window = make_window(qtbot, env, save_settings=fail)
        try:
            window._remember_attachment_import_directory(str(tmp_path))
            assert window._settings.attachment_import_directory == str(tmp_path)
            assert "저장하지 못" in window.statusBar().currentMessage()
        finally:
            window.shutdown()


def test_picker_missing_local_directory_falls_back_without_network_probe(
    qapp, tmp_path, monkeypatch
):
    from PySide6.QtCore import QStandardPaths

    import officeflow.presentation.attachment_import as module

    default = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    assert import_start_directory(str(tmp_path)) == str(tmp_path)
    assert import_start_directory(str(tmp_path / "absent")) == default
    monkeypatch.setattr(module, "_remote_directory", lambda _path: True)
    monkeypatch.setattr(
        Path, "is_dir", lambda _path: (_ for _ in ()).throw(AssertionError("network stat"))
    )
    assert import_start_directory(str(tmp_path / "remote")) == str(tmp_path / "remote")


def test_description_empty_long_and_schedule_live_tooltip(qtbot, tmp_path):
    with search_environment(tmp_path) as env:
        start = datetime(2026, 10, 20, 3, tzinfo=UTC)
        task = env.tasks.create(
            TaskDraft(
                "description",
                description="\n".join(f"Line {i} " + "long " * 40 for i in range(20)),
                starts_at=start,
            )
        )
        clock = [start - timedelta(days=6)]
        records = TaskRecordsDialog(
            task, task_service=env.tasks, record_service=env.records, clock=lambda: clock[0]
        )
        qtbot.addWidget(records)
        records.resize(500, 540)
        records.show()
        qtbot.wait(30)
        panel = records.description_panel
        assert panel.body.height() <= 3 * panel.body.fontMetrics().lineSpacing() + 16
        assert panel.toggle.text() == "더 보기"
        panel.toggle.click()
        assert panel.body.height() <= 6 * panel.body.fontMetrics().lineSpacing() + 16
        assert panel.body.toPlainText() == task.description
        clock[0] = start - timedelta(days=1)
        point = records.schedule_label.rect().center()
        event = QHelpEvent(QEvent.Type.ToolTip, point, records.schedule_label.mapToGlobal(point))
        records.schedule_label.event(event)
        assert "시작일은 1일 후" in records.schedule_label.toolTip()
        env.tasks.update(task.id, TaskDraft("description", description=" "))
        records.refresh_header()
        assert not panel.isVisible()
