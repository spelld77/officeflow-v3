from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QDesktopServices
from pytestqt.qtbot import QtBot

from officeflow.application.attachment_search import (
    AttachmentCursor,
    AttachmentSearchHit,
    AttachmentSearchInterrupted,
    AttachmentSearchPage,
    AttachmentSearchService,
)
from officeflow.application.reminders import ReminderService
from officeflow.application.tasks import TaskDraft, TaskView
from officeflow.domain.enums import ReminderRelation, TaskStatus
from officeflow.domain.reminder import ReminderRuleInput
from officeflow.infrastructure.database.reminder_repository import SqlAlchemyReminderRepository
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.attachment_search_page import AttachmentSearchPageWidget
from officeflow.presentation.main_window import MainWindow
from officeflow.presentation.record_dialog import WorkLogBrowserDialog
from tests.integration.test_attachment_search import NOW, search_environment


def window_for(env, qtbot: QtBot, *, reminders=None) -> MainWindow:
    window = MainWindow(
        AppSettings(automatic_backup_enabled=False),
        env.tasks,
        record_service=env.records,
        attachment_service=env.attachments,
        attachment_search_service=env.search,
        reminder_service=reminders,
    )
    qtbot.addWidget(window)
    window.show()
    return window


def wait_page(window: MainWindow, qtbot: QtBot, count: int) -> AttachmentSearchPageWidget:
    page = window._file_page
    assert page is not None
    qtbot.waitUntil(
        lambda: (
            page.model.rowCount() == count and page._worker is None and not page._timer.isActive()
        ),
        timeout=5000,
    )
    return page


def test_global_search_and_return_preserve_view_filters_and_clear_query(
    qtbot: QtBot, tmp_path: Path
) -> None:
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="지난 업무", status=TaskStatus.COMPLETED))
        env.add(task.id, "장비견적서.xlsx")
        window = window_for(env, qtbot)
        window._pinned_filter.setChecked(True)
        window._search.setText("견적")
        window._search_mode.setCurrentIndex(1)
        page = wait_page(window, qtbot, 1)
        assert window._content_stack.currentWidget() is page
        assert "전체 기간" in page.scope_label.text()
        assert page.model.items[0].task_status is TaskStatus.COMPLETED
        assert not window._detail_panel.isVisible()
        qtbot.mouseClick(page.back_button, Qt.MouseButton.LeftButton)
        assert window._current_view is TaskView.TODAY
        assert window._pinned_filter.isChecked()
        assert not window._search.text()
        window._show_calendar()
        selected_date = window._calendar_page.selected_date
        window._search_mode.setCurrentIndex(1)
        wait_page(window, qtbot, 1)
        qtbot.mouseClick(page.back_button, Qt.MouseButton.LeftButton)
        assert window._calendar_active and window._calendar_page.selected_date == selected_date
        window.close()


@pytest.mark.parametrize("size", [(760, 560), (960, 640), (1280, 800)])
def test_layout_short_query_and_clear_additional_conditions(
    qtbot: QtBot, tmp_path: Path, size, request
) -> None:
    if os.environ.get("OFFICEFLOW_TEST_SCREENSHOT_DIR"):
        from PySide6.QtGui import QFontDatabase

        font_ids = []
        for filename in ("malgun.ttf", "malgunbd.ttf"):
            font_file = Path("C:/Windows/Fonts") / filename
            if font_file.is_file():
                font_id = QFontDatabase.addApplicationFont(str(font_file))
                assert font_id >= 0
                font_ids.append(font_id)

        def remove_test_fonts():
            for font_id in font_ids:
                QFontDatabase.removeApplicationFont(font_id)

        request.addfinalizer(remove_test_fonts)
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="계약"))
        env.add(task.id, "견적보고서.xlsx")
        window = window_for(env, qtbot)
        window.resize(*size)
        window._search_mode.setCurrentIndex(1)
        page = wait_page(window, qtbot, 1)
        qtbot.mouseClick(page.more_conditions, Qt.MouseButton.LeftButton)
        page.trash_check.setChecked(True)
        page.detached_check.setChecked(True)
        page.range_check.setChecked(True)
        wait_page(window, qtbot, 1)
        for widget in (
            page.back_button,
            page.open_button,
            page.task_button,
            page.manage_button,
            page.date_to,
        ):
            assert page.rect().contains(widget.mapTo(page, widget.rect().bottomRight()))
        assert window._search.width() >= 100
        for button in (page.more_conditions, page.back_button, page.clear_button):
            assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 24
        for button in window._nav_buttons:
            assert button.height() >= button.fontMetrics().height() + 8
        if folder := os.environ.get("OFFICEFLOW_TEST_SCREENSHOT_DIR"):
            destination = Path(folder)
            destination.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(destination / f"attachment-search-{size[0]}x{size[1]}.png"))
        window._search.setText("견")
        qtbot.waitUntil(lambda: "2글자" in page.feedback.text())
        assert not page.model.items
        assert not page.open_button.isEnabled()
        qtbot.mouseClick(page.clear_button, Qt.MouseButton.LeftButton)
        wait_page(window, qtbot, 1)
        assert not window._search.text()
        assert not page.range_check.isChecked() and not page.trash_check.isChecked()
        assert "전체 기간" in page.scope_label.text()
        window.close()


def test_open_file_keyboard_missing_delete_and_read_only_manage(
    qtbot: QtBot, tmp_path: Path, monkeypatch
) -> None:
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="파일 열기"))
        source = tmp_path / "업무보고서.txt"
        source.write_text("synthetic test", encoding="utf-8")
        item = env.attachments.attach(task.id, source)
        window = window_for(env, qtbot)
        window._search_mode.setCurrentIndex(1)
        page = wait_page(window, qtbot, 1)
        page.list_view.setCurrentIndex(page.model.index(0, 0))
        opened = []
        monkeypatch.setattr(
            QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True
        )
        qtbot.mouseClick(page.open_button, Qt.MouseButton.LeftButton)
        assert [Path(path) for path in opened] == [env.attachments.path_for_open(item.id_required)]
        page.list_view.setFocus()
        qtbot.keyClick(page.list_view, Qt.Key.Key_Return)
        assert len(opened) == 2
        qtbot.mouseClick(page.manage_button, Qt.MouseButton.LeftButton)
        assert window._file_dialogs
        dialog = window._file_dialogs[0]
        assert not dialog.isModal()
        assert dialog._selected_attachment_id == item.id
        dialog.reject()
        wait_page(window, qtbot, 1)
        env.tasks.move_to_trash(task.id)
        page.trash_check.setChecked(True)
        wait_page(window, qtbot, 1)
        page.list_view.setCurrentIndex(page.model.index(0, 0))
        qtbot.mouseClick(page.manage_button, Qt.MouseButton.LeftButton)
        dialog = window._file_dialogs[0]
        assert dialog._read_only and not dialog.add_attachment_button.isEnabled()
        dialog.reject()
        wait_page(window, qtbot, 1)
        env.attachments.path_for_open(item.id_required).unlink()
        page.list_view.setCurrentIndex(page.model.index(0, 0))
        qtbot.mouseClick(page.open_button, Qt.MouseButton.LeftButton)
        wait_page(window, qtbot, 1)
        assert page.model.items[0].missing
        assert len(opened) == 2
        env.attachments.delete_file(item.id_required)
        page.list_view.setCurrentIndex(page.model.index(0, 0))
        qtbot.mouseClick(page.open_button, Qt.MouseButton.LeftButton)
        wait_page(window, qtbot, 0)
        assert not page.open_button.isEnabled()
        window.close()


class FakeSearchRepository:
    def __init__(self, count=1, *, slow=False):
        self.count, self.slow = count, slow
        self.running = 0
        self.maximum_running = 0

    def search_page(self, query, *, cancel_requested=None):
        self.running += 1
        self.maximum_running = max(self.running, self.maximum_running)
        try:
            until = monotonic() + (0.15 if self.slow else 0)
            while monotonic() < until:
                if cancel_requested and cancel_requested():
                    raise AttachmentSearchInterrupted("검색 취소")
            top = query.cursor.attachment_id - 1 if query.cursor else self.count
            ids = list(range(top, max(0, top - query.limit), -1))
            items = tuple(
                AttachmentSearchHit(
                    i,
                    1,
                    "업무",
                    TaskStatus.COMPLETED,
                    f"{query.search or '첨부'}-{i}.xlsx",
                    42,
                    NOW,
                    False,
                    False,
                    False,
                )
                for i in ids
            )
            cursor = AttachmentCursor(NOW, ids[-1]) if ids else None
            return AttachmentSearchPage(items, cursor, bool(ids and ids[-1] > 1))
        finally:
            self.running -= 1


def test_latest_query_wins_worker_is_single_and_event_loop_runs(qtbot: QtBot) -> None:
    repository = FakeSearchRepository(slow=True)
    page = AttachmentSearchPageWidget(AttachmentSearchService(repository), "Asia/Seoul")
    qtbot.addWidget(page)
    page.show()
    heartbeats = []
    timer = QTimer(page)
    timer.setInterval(10)
    timer.timeout.connect(lambda: heartbeats.append(True))
    timer.start()
    page.activate("이전")
    qtbot.waitUntil(lambda: page._worker is not None)
    page.set_search("새검색")
    qtbot.waitUntil(
        lambda: bool(page.model.items) and page.model.items[0].original_name.startswith("새검색"),
        timeout=3000,
    )
    assert repository.maximum_running == 1 and len(heartbeats) > 5
    page.set_search("종료")
    page.deactivate()
    qtbot.waitUntil(lambda: page._worker is None)
    assert not page.model.items
    page.shutdown()


def test_paging_has_bounded_rows_and_previous_block(qtbot: QtBot) -> None:
    page = AttachmentSearchPageWidget(
        AttachmentSearchService(FakeSearchRepository(505)), "Asia/Seoul"
    )
    qtbot.addWidget(page)
    page.activate("")
    qtbot.waitUntil(lambda: len(page.model.items) == 50 and page._worker is None)
    for count in range(100, 501, 50):
        page._next()
        qtbot.waitUntil(
            lambda expected=count: len(page.model.items) == expected and page._worker is None
        )
    assert page.next_button.text() == "다음 구간"
    page._next()
    qtbot.waitUntil(lambda: len(page.model.items) == 5 and page._worker is None)
    assert "501~505" in page.feedback.text()
    assert not page.next_button.isEnabled()
    page._previous()
    qtbot.waitUntil(lambda: len(page.model.items) == 50 and page._worker is None)
    assert page.model.items[0].attachment_id == 505
    page.shutdown()


def test_work_log_reports_filename_match(qtbot: QtBot, tmp_path: Path) -> None:
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="완료 기록", status=TaskStatus.COMPLETED))
        env.add(task.id, "납품검수보고서.xlsx")
        dialog = WorkLogBrowserDialog(
            task_service=env.tasks, record_service=env.records, attachment_service=env.attachments
        )
        qtbot.addWidget(dialog)
        dialog.search_edit.setText("검수")
        qtbot.waitUntil(
            lambda: (
                dialog.list_widget.count() == 1
                and "첨부파일명" in dialog.list_widget.item(0).text()
            )
        )
        dialog.close()


def test_task_search_direct_matching_file_and_access_failure(
    qtbot: QtBot, tmp_path: Path, monkeypatch
) -> None:
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="검색 업무"))
        source = tmp_path / "구매견적서.txt"
        source.write_text("synthetic", encoding="utf-8")
        item = env.attachments.attach(task.id, source)
        window = window_for(env, qtbot)
        window.resize(1440, 900)
        window._set_view(TaskView.ALL)
        window._search.setText("견적")
        qtbot.waitUntil(
            lambda: window._task_model.rowCount() == 1 and not window._search_timer.isActive()
        )
        window._task_list.setCurrentIndex(window._task_model.index(0, 0))
        assert window._matched_file_button.isVisible()
        assert window._matched_file_button.property("attachmentId") == item.id
        assert "구매견적서" in window._detail_schedule.text()
        monkeypatch.setattr(QDesktopServices, "openUrl", lambda _url: False)
        qtbot.mouseClick(window._matched_file_button, Qt.MouseButton.LeftButton)
        assert "접근 권한" in window.statusBar().currentMessage()
        window._search.clear()
        window._refresh_tasks()
        assert not window._matched_file_button.isVisible()
        window.close()


def test_detached_management_is_modeless_and_closes_cleanly(qtbot: QtBot, tmp_path: Path) -> None:
    from officeflow.application.exporting import ExportService
    from officeflow.bootstrap.paths import AppPaths
    from officeflow.infrastructure.backup import BackupManager
    from officeflow.infrastructure.exports.calendar import ICalendarTaskExporter
    from officeflow.infrastructure.exports.excel import ExcelTaskExporter

    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="해제한 업무"))
        item = env.add(task.id, "해제보고서.xlsx")
        env.attachments.unlink(item.id_required)
        window = window_for(env, qtbot)
        paths = AppPaths(tmp_path / "managed")
        paths.ensure_directories()
        window._backup_manager = BackupManager(paths)
        window._export_service = ExportService(
            env.tasks, ExcelTaskExporter(), ICalendarTaskExporter()
        )
        window._search_mode.setCurrentIndex(1)
        page = window._file_page
        page.detached_check.setChecked(True)
        wait_page(window, qtbot, 1)
        page.list_view.setCurrentIndex(page.model.index(0, 0))
        qtbot.mouseClick(page.manage_button, Qt.MouseButton.LeftButton)
        dialog = window._file_dialogs[0]
        assert not dialog.isModal()
        assert dialog.tabs.currentIndex() == dialog._cleanup_tab_index
        window.shutdown()
        assert not dialog.isVisible()
        assert not window._file_dialogs
        window.close()


def test_startup_upgrade_dialog_stays_responsive_and_propagates_failure(qtbot: QtBot) -> None:
    from officeflow.presentation.database_upgrade import DatabaseUpgradeDialog

    heartbeats = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: heartbeats.append(True))
    timer.start()

    def operation(progress):
        from threading import Event

        progress("합성 색인 준비")
        Event().wait(0.2)

    dialog = DatabaseUpgradeDialog(operation)
    qtbot.addWidget(dialog)
    QTimer.singleShot(20, dialog.reject)
    dialog.run_upgrade()
    assert dialog.label.text() == "합성 색인 준비"
    assert len(heartbeats) > 3 and not dialog.worker.isRunning()

    def failing_operation(_progress):
        raise OSError("synthetic upgrade failure")

    failed = DatabaseUpgradeDialog(failing_operation)
    qtbot.addWidget(failed)
    with pytest.raises(OSError, match="synthetic"):
        failed.run_upgrade()
    assert not failed.worker.isRunning()
    timer.stop()


def test_bootstrap_upgrades_old_database_before_search(
    qtbot: QtBot, qapp, tmp_path: Path, monkeypatch
) -> None:
    from alembic import command

    from officeflow.bootstrap.paths import AppPaths
    from officeflow.infrastructure.database.migrate import migration_config
    from officeflow.infrastructure.settings.store import JsonSettingsStore
    from officeflow.main import build_application

    paths = AppPaths(tmp_path / "bootstrap")
    paths.ensure_directories()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        task = env.tasks.create(TaskDraft(title="이전 업무"))
        env.add(task.id, "이전보고서.pdf")
    command.downgrade(migration_config(paths.database_file), "0009_reminder_schedule_cache")
    JsonSettingsStore(paths.settings_file).save(AppSettings(automatic_backup_enabled=False))
    monkeypatch.setenv("OFFICEFLOW_DATA_DIR", str(paths.root))
    _, window = build_application(application=qapp)
    qtbot.addWidget(window)
    window.show()
    window._search_mode.setCurrentIndex(1)
    page = wait_page(window, qtbot, 1)
    assert page.model.items[0].original_name == "이전보고서.pdf"
    assert list((paths.data_dir / "schema-backups").glob("*.db"))
    window.close()


def test_shutdown_waits_for_modeless_attachment_import(qtbot: QtBot, tmp_path: Path, monkeypatch) -> None:
    from threading import Event

    from officeflow.application.attachments import AttachmentCanceledError
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="종료 검증"))
        env.add(task.id, "기존보고서.xlsx")
        window = window_for(env, qtbot)
        window._search_mode.setCurrentIndex(1)
        page = wait_page(window, qtbot, 1)
        page.list_view.setCurrentIndex(page.model.index(0, 0))
        qtbot.mouseClick(page.manage_button, Qt.MouseButton.LeftButton)
        record = window._file_dialogs[0]
        started = Event()
        finished = Event()

        def import_file(_task_id, _source, *, cancel_requested):
            started.set()
            for _ in range(100):
                if cancel_requested():
                    finished.set()
                    raise AttachmentCanceledError("취소")
                Event().wait(.01)
            raise AssertionError("shutdown did not cancel import")

        monkeypatch.setattr(env.attachments, "attach", import_file)
        source = tmp_path / "추가파일.txt"
        source.write_bytes(b"synthetic")
        record._start_attachment_imports((source,))
        qtbot.waitUntil(started.is_set)
        thread = record._attachment_thread
        window.shutdown()
        assert finished.is_set() and not thread.isRunning()
        qtbot.wait(20)
        assert not window._file_dialogs
        window.close()


def test_startup_failure_is_visible_and_releases_instance(qapp, tmp_path: Path, monkeypatch) -> None:
    import officeflow.main as entry
    events = []

    class FakeCoordinator:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self):
            return True

        def close(self):
            events.append("released")

    def fail(*_args, **_kwargs):
        raise OSError("synthetic disk full")

    monkeypatch.setenv("OFFICEFLOW_DATA_DIR", str(tmp_path / "start-failure"))
    monkeypatch.setattr(entry, "QApplication", lambda _args: qapp)
    monkeypatch.setattr(entry, "SingleInstanceCoordinator", FakeCoordinator)
    monkeypatch.setattr(entry, "build_application", fail)
    monkeypatch.setattr(entry.sys, "argv", ["officeflow"])
    monkeypatch.setattr(entry.QMessageBox, "critical", lambda _parent, _title, message: events.append(message))
    assert entry.main() == 1
    assert "synthetic disk full" in events[0] and events[1] == "released"
    assert not (tmp_path / "start-failure" / "officeflow.running").exists()


def test_reminder_and_file_dialog_are_both_clickable(qtbot: QtBot, tmp_path: Path) -> None:
    with search_environment(tmp_path) as env:
        task = env.tasks.create(
            TaskDraft(title="알림 업무", starts_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        env.add(task.id, "알림보고서.xlsx")
        reminders = ReminderService(SqlAlchemyReminderRepository(env.sessions), env.tasks)
        window = window_for(env, qtbot, reminders=reminders)
        window._search_mode.setCurrentIndex(1)
        page = wait_page(window, qtbot, 1)
        page.list_view.setCurrentIndex(page.model.index(0, 0))
        qtbot.mouseClick(page.manage_button, Qt.MouseButton.LeftButton)
        record = window._file_dialogs[0]
        reminders.replace_rules(
            task.id, (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),)
        )
        window._check_reminders()
        alert = window._reminder_dialog
        assert alert is not None and alert.isVisible()
        assert not record.isModal()
        qtbot.mouseClick(alert.acknowledge_button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: window._reminder_dialog is None)
        assert record.isVisible()
        record.reject()
        assert not window._file_dialogs
        window.close()
