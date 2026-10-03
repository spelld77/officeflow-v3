"""Record windows must not filter native input to independent reminder windows."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton
from pytestqt.qtbot import QtBot
from shiboken6 import isValid

from officeflow.application.attachments import AttachmentCanceledError
from officeflow.application.reminders import ReminderService
from officeflow.application.tasks import TaskDraft, TaskView
from officeflow.domain.enums import ReminderDeliveryStatus, ReminderRelation, TaskStatus
from officeflow.domain.reminder import ReminderRuleInput
from officeflow.infrastructure.database.reminder_repository import SqlAlchemyReminderRepository
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow
from officeflow.presentation.record_dialog import TaskRecordsDialog, WorkLogBrowserDialog
from tests.integration.test_attachment_search import SearchEnvironment, search_environment
from tests.ui.test_attachment_search_page import wait_page
from tests.ui.test_hourly_notification_ui import FakeClock, controller

PATHS = (
    "list-records",
    "list-attachments",
    "calendar-records",
    "calendar-attachments",
    "file-records",
    "file-attachments",
    "work-log",
    "work-log-records",
)


@pytest.fixture
def record_window(
    qtbot: QtBot, tmp_path: Path, monkeypatch
) -> Iterator[tuple[MainWindow, SearchEnvironment, int, ReminderService]]:
    def unexpected_exec(_dialog):
        raise AssertionError("Record windows must not enter a modal event loop")

    def unexpected_error(_window, _message, error):
        raise error

    def unexpected_warning(_parent, _title, message):
        raise AssertionError(message)

    monkeypatch.setattr(TaskRecordsDialog, "exec", unexpected_exec)
    monkeypatch.setattr(WorkLogBrowserDialog, "exec", unexpected_exec)
    monkeypatch.setattr(MainWindow, "_show_error", unexpected_error)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", unexpected_warning)
    with search_environment(tmp_path) as env:
        now = datetime.now(UTC)
        task = env.tasks.create(TaskDraft(title="알림과 기록", starts_at=now - timedelta(minutes=1)))
        assert task.id is not None
        env.add(task.id, "기록보고서.txt")
        env.records.add_work_log(
            task_id=task.id,
            log_date=now.astimezone(ZoneInfo("Asia/Seoul")).date(),
            content="작성한 업무일지",
        )
        reminders = ReminderService(SqlAlchemyReminderRepository(env.sessions), env.tasks)
        window = MainWindow(
            AppSettings(automatic_backup_enabled=False),
            env.tasks,
            record_service=env.records,
            attachment_service=env.attachments,
            attachment_search_service=env.search,
            reminder_service=reminders,
            hourly_notifications_enabled=False,
        )
        qtbot.addWidget(window)
        window.show()
        try:
            yield window, env, task.id, reminders
        finally:
            window.shutdown()
            window.close()
            qtbot.wait(10)


def open_record_window(window: MainWindow, task_id: int, path: str, qtbot: QtBot) -> QDialog:
    if path.startswith("work-log"):
        window._open_work_logs()
        browser = window._file_dialogs[-1]
        assert isinstance(browser, WorkLogBrowserDialog)
        if path == "work-log":
            return browser
        browser.list_widget.setCurrentRow(0)
        browser.open_records_button.click()
        return browser._record_dialogs[-1]
    if path.startswith("file"):
        window._search_mode.setCurrentIndex(1)
        page = wait_page(window, qtbot, 1)
        page.list_view.setCurrentIndex(page.model.index(0, 0))
        (page.manage_button if path.endswith("attachments") else page.task_button).click()
        return window._file_dialogs[-1]
    if path.startswith("calendar"):
        window._show_calendar()
        window._calendar_page.day_list.setCurrentRow(0)
    else:
        window._set_view(TaskView.ALL)
        window._task_list.setCurrentIndex(window._task_model.index_for_task(task_id))
    assert window._selected_task_id == task_id
    task = window._task_service.get(task_id)
    menu = window._build_task_context_menu(task)
    label = "첨부파일 보기 · 관리…" if path.endswith("attachments") else "완료 요약 · 기록 열기"
    next(action for action in menu.actions() if action.text() == label).trigger()
    menu.deleteLater()
    return window._file_dialogs[-1]


def native_click(dialog: QDialog, button: QPushButton, qtbot: QtBot) -> None:
    qtbot.waitExposed(dialog)
    handle = dialog.windowHandle()
    assert handle is not None
    # QWidget clicks bypass Qt's modal-window filter; use real QWindow dispatch.
    QTest.mouseClick(
        handle,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        button.mapTo(dialog, button.rect().center()),
    )


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("notification", ["task", "hourly"])
def test_native_reminder_click_works_with_every_record_entry_path(
    record_window, qtbot: QtBot, path: str, notification: str
) -> None:
    window, _env, task_id, reminders = record_window
    record = open_record_window(window, task_id, path, qtbot)
    assert record.isVisible() and not record.isModal()
    assert QApplication.activeModalWidget() is None
    if isinstance(record, TaskRecordsDialog):
        record.result_edit.setPlainText("아직 저장하지 않은 입력")
    if notification == "task":
        reminders.replace_rules(task_id, (ReminderRuleInput(ReminderRelation.START, 0),))
        window._check_reminders()
        alert = window._reminder_dialog
        assert alert is not None and alert.isVisible()
        native_click(alert, alert.acknowledge_button, qtbot)
        qtbot.waitUntil(lambda: window._reminder_dialog is None)
    else:
        control = controller(qtbot, FakeClock())
        try:
            alert = control.dialog
            assert alert is not None and alert.isVisible()
            native_click(alert, alert.dismiss_button, qtbot)
            qtbot.waitUntil(lambda: not alert.isVisible())
            assert control.service.state(control.clock.now()).current_status == "closed"
        finally:
            control.shutdown()
    assert record.isVisible()
    if isinstance(record, TaskRecordsDialog):
        assert record.result_edit.toPlainText() == "아직 저장하지 않은 입력"
    record.reject()
    qtbot.waitUntil(lambda: not isValid(record))


@pytest.mark.parametrize("action", ["complete", "defer", "snooze"])
def test_task_reminder_actions_update_data_without_closing_record(
    record_window, qtbot: QtBot, action: str
) -> None:
    window, env, task_id, reminders = record_window
    record = open_record_window(window, task_id, "list-attachments", qtbot)
    reminders.replace_rules(task_id, (ReminderRuleInput(ReminderRelation.START, 0),))
    window._check_reminders()
    alert = window._reminder_dialog
    assert alert is not None
    native_click(alert, getattr(alert, f"{action}_button"), qtbot)
    qtbot.waitUntil(lambda: window._reminder_dialog is None)
    assert record.isVisible()
    if action == "snooze":
        waiting = reminders.active_snoozes()
        assert len(waiting) == 1
        assert waiting[0].status is ReminderDeliveryStatus.SNOOZED
    else:
        expected = TaskStatus.COMPLETED if action == "complete" else TaskStatus.PENDING
        assert env.tasks.get(task_id).status is expected
    assert window._task_model.task_at(window._task_model.index_for_task(task_id)) == env.tasks.get(task_id)


@pytest.mark.parametrize("notification", ["task", "hourly"])
def test_native_reminder_can_be_processed_during_attachment_verification(
    record_window, qtbot: QtBot, monkeypatch, notification: str
) -> None:
    from time import sleep

    window, env, task_id, reminders = record_window
    entered = Event()

    def verify(_attachment_id, *, cancel_requested=None, **_kwargs):
        entered.set()
        while not cancel_requested():
            sleep(0.005)
        raise AttachmentCanceledError("검사 취소됨")

    monkeypatch.setattr(env.attachments, "verify", verify)
    record = open_record_window(window, task_id, "list-attachments", qtbot)
    assert isinstance(record, TaskRecordsDialog)
    record.attachment_list.setCurrentRow(0)
    record._verify_attachment()
    qtbot.waitUntil(entered.is_set)
    assert not record._verification_progress.isModal()
    control = None
    try:
        if notification == "task":
            reminders.replace_rules(task_id, (ReminderRuleInput(ReminderRelation.START, 0),))
            window._check_reminders()
            alert = window._reminder_dialog
            assert alert is not None
            native_click(alert, alert.acknowledge_button, qtbot)
            qtbot.waitUntil(lambda: window._reminder_dialog is None)
        else:
            control = controller(qtbot, FakeClock())
            alert = control.dialog
            assert alert is not None
            native_click(alert, alert.dismiss_button, qtbot)
            qtbot.waitUntil(lambda: not alert.isVisible())
        assert record.isVisible() and record._verification_thread is not None
    finally:
        if control is not None:
            control.shutdown()
        record.shutdown()


def test_nested_record_reuses_window_propagates_changes_and_refreshes_main(
    record_window, qtbot: QtBot
) -> None:
    window, env, task_id, _reminders = record_window
    record = open_record_window(window, task_id, "work-log-records", qtbot)
    assert isinstance(record, TaskRecordsDialog)
    browser = window._file_dialogs[-1]
    assert isinstance(browser, WorkLogBrowserDialog)
    changed = []
    browser.changed.connect(lambda: changed.append(True))
    browser.open_records_button.click()
    assert browser._record_dialogs == [record]
    record.result_edit.setPlainText("저장한 완료 요약")
    record._save_result()
    assert env.tasks.result_note(task_id) == "저장한 완료 요약"
    assert "저장한 완료 요약" in browser.detail.toPlainText()
    assert changed == [True]
    assert window._task_model.task_at(window._task_model.index_for_task(task_id)) == env.tasks.get(task_id)
    record.reject()
    assert not browser._record_dialogs
    qtbot.waitUntil(lambda: not isValid(record))
    browser.close()
    assert not window._file_dialogs
    qtbot.waitUntil(lambda: not isValid(browser))


@pytest.mark.parametrize("close_action", ["browser-close", "browser-reject", "main-shutdown"])
def test_nested_record_import_is_canceled_before_owner_closes(
    record_window, qtbot: QtBot, tmp_path: Path, monkeypatch, close_action: str
) -> None:
    window, env, task_id, _reminders = record_window
    record = open_record_window(window, task_id, "work-log-records", qtbot)
    assert isinstance(record, TaskRecordsDialog)
    browser = window._file_dialogs[-1]
    assert isinstance(browser, WorkLogBrowserDialog)
    started, finished = Event(), Event()

    def import_file(_task_id, _source, *, cancel_requested):
        started.set()
        for _ in range(200):
            if cancel_requested():
                finished.set()
                raise AttachmentCanceledError("취소")
            Event().wait(.01)
        raise AssertionError("Owner shutdown did not cancel file import")

    monkeypatch.setattr(env.attachments, "attach", import_file)
    record._start_attachment_imports((tmp_path / "합성파일.txt",))
    qtbot.waitUntil(started.is_set)
    thread = record._attachment_thread
    assert thread is not None
    if close_action == "main-shutdown":
        window.shutdown()
    elif close_action == "browser-close":
        browser.close()
    else:
        browser.reject()
    assert finished.is_set() and not thread.isRunning()
    assert not browser._record_dialogs and not window._file_dialogs
    qtbot.waitUntil(lambda: not isValid(record) and not isValid(browser))


def test_task_reminder_is_clickable_while_record_copies_attachment(
    record_window, qtbot: QtBot, tmp_path: Path, monkeypatch
) -> None:
    window, env, task_id, reminders = record_window
    record = open_record_window(window, task_id, "list-attachments", qtbot)
    assert isinstance(record, TaskRecordsDialog)
    started, finished = Event(), Event()

    def import_file(_task_id, _source, *, cancel_requested):
        started.set()
        for _ in range(200):
            if cancel_requested():
                finished.set()
                raise AttachmentCanceledError("취소")
            Event().wait(.01)
        raise AssertionError("File import was not canceled")

    monkeypatch.setattr(env.attachments, "attach", import_file)
    record._start_attachment_imports((tmp_path / "합성파일.txt",))
    qtbot.waitUntil(started.is_set)
    reminders.replace_rules(task_id, (ReminderRuleInput(ReminderRelation.START, 0),))
    window._check_reminders()
    alert = window._reminder_dialog
    assert alert is not None
    native_click(alert, alert.acknowledge_button, qtbot)
    qtbot.waitUntil(lambda: window._reminder_dialog is None)
    assert record.isVisible() and record._attachment_thread is not None
    record.reject()
    qtbot.waitUntil(finished.is_set)
    qtbot.waitUntil(lambda: not window._file_dialogs)
