from datetime import UTC, date, datetime, timedelta

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMessageBox

from officeflow.application.reminder_groups import ReminderGroup
from officeflow.application.reminder_preview import ReminderPreviewService
from officeflow.application.reminders import ReminderService
from officeflow.application.task_saving import ReminderEdit
from officeflow.application.tasks import TaskDraft
from officeflow.domain.enums import ReminderDeliveryStatus, ReminderRelation, TaskStatus
from officeflow.domain.reminder import ReminderRuleInput as Rule
from officeflow.domain.task import Task
from officeflow.infrastructure.database.reminder_repository import SqlAlchemyReminderRepository
from officeflow.infrastructure.database.task_saving import task_save_service
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.dates import format_date, format_datetime
from officeflow.presentation.main_window import MainWindow
from officeflow.presentation.reminder_settings import ReminderRuleDialog, ReminderSettingsDialog
from officeflow.presentation.task_editor import TaskEditorDialog
from tests.integration.test_attachment_search import search_environment


@pytest.fixture
def live_window(qtbot, tmp_path, monkeypatch):
    with search_environment(tmp_path) as env:
        repo = SqlAlchemyReminderRepository(env.sessions)
        service = ReminderService(repo, env.tasks)
        save = task_save_service(env.sessions, "Asia/Seoul")
        now = (datetime.now(UTC) + timedelta(days=1)).replace(
            hour=1, minute=0, second=0, microsecond=0
        )
        monkeypatch.setattr(MainWindow, "_start_reminder_monitoring", lambda self: None)
        task = save.save(
            TaskDraft("한달 뒤 제출", starts_at=now + timedelta(days=30)),
            edits=tuple(
                ReminderEdit(
                    Rule(ReminderRelation.ABSOLUTE, absolute_at=now + timedelta(minutes=i))
                )
                for i in range(5)
            ),
            now=now,
        )
        window = MainWindow(
            AppSettings(),
            env.tasks,
            reminder_service=service,
            task_save_service=save,
            reminder_preview_service=ReminderPreviewService(repo, service),
            record_service=env.records,
            attachment_service=env.attachments,
            view_clock=lambda: now,
        )
        qtbot.addWidget(window)
        window.show()
        try:
            yield window, env, task, now, repo
        finally:
            window.shutdown()


def test_today_reminders_independent_of_task_filters_and_overflow(live_window, qtbot):
    window, _env, _task, _now, _repo = live_window
    qtbot.waitUntil(lambda: len(window._today_reminders._result.items) == 5, timeout=5000)
    qtbot.wait(80)
    assert window._task_model.total_task_count == 0
    assert "총 5건" in window._today_reminders.label.text()
    assert window._today_reminders.list.count() == 1
    assert window._today_reminders.more.text() == "전체 보기"
    window._search.setText("없는 검색어")
    window._refresh_tasks()
    qtbot.waitUntil(lambda: window._preview_controller._thread is None, timeout=5000)
    assert len(window._today_reminders._result.items) == 5
    qtbot.mouseClick(window._today_reminders.more, Qt.MouseButton.LeftButton)
    assert window._today_reminders._dialog is not None
    window._today_reminders._dialog.reject()


def test_calendar_alarm_dates_and_underlying_records(live_window, qtbot):
    window, _env, task, now, _repo = live_window
    window._show_calendar()
    page = window._calendar_page
    from zoneinfo import ZoneInfo

    day = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    page._set_month_and_date(day)
    qtbot.waitUntil(lambda: page.calendar._alarm_counts.get(day) == 5, timeout=5000)
    assert page.day_list.count() == 1
    data = page.day_list.item(0).data(Qt.ItemDataRole.UserRole)
    assert isinstance(data, ReminderGroup) and len(data.items) == 5
    page.day_list.setCurrentRow(0)
    assert window._selected_task_id == task.id
    page._on_day_item_activated(page.day_list.item(0))
    assert any(d.windowTitle() for d in window._file_dialogs)


def test_task_save_error_keeps_editor_open_and_inputs(qtbot):
    def fail(editor):
        raise OSError("저장 실패 테스트")

    editor = TaskEditorDialog(timezone="Asia/Seoul", on_save=fail)
    qtbot.addWidget(editor)
    editor.show()
    editor.title_edit.setText("보존할 입력")
    qtbot.mouseClick(editor.save_button, Qt.MouseButton.LeftButton)
    assert editor.isVisible()
    assert editor.title_edit.text() == "보존할 입력"
    assert "저장 실패" in editor.error_label.text()


def test_settings_cancel_does_not_change_parent_draft_and_blocks_parent_save(qtbot):
    editor = TaskEditorDialog(timezone="Asia/Seoul")
    qtbot.addWidget(editor)
    editor.show()
    original = editor.reminder_edits()
    qtbot.mouseClick(editor.reminder_settings_button, Qt.MouseButton.LeftButton)
    settings = editor._reminder_settings
    assert settings is not None
    assert not settings.isModal()
    assert not editor.save_button.isEnabled()
    settings.list.setCurrentRow(0)
    settings._delete()
    settings.reject()
    assert editor.reminder_edits() == original
    assert editor.save_button.isEnabled()


def test_settings_limit_and_modeless_child(qtbot):
    task = Task.create(title="알림 20개", starts_at=datetime.now(UTC) + timedelta(days=10))
    edits = tuple(
        ReminderEdit(Rule(ReminderRelation.START, offset_minutes=-i), i + 1) for i in range(20)
    )
    settings = ReminderSettingsDialog(task, edits)
    qtbot.addWidget(settings)
    settings.show()
    assert not settings.add_button.isEnabled()
    assert settings.edit_button.isEnabled()
    settings._open_rule(True)
    assert settings._child is not None
    assert not settings._child.isModal()
    settings.reject()
    assert settings._child is None


def test_absolute_default_weekday_and_tab_flow(qtbot):
    task = Task.create(title="입력", starts_at=datetime.now(UTC) + timedelta(days=10))
    dialog = ReminderRuleDialog(task)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.mode.currentData() == "absolute"
    dialog.date_edit.setFocus()
    qtbot.keyPress(dialog.date_edit, Qt.Key.Key_Tab)
    assert dialog.focusWidget() is dialog.time_edit
    qtbot.keyPress(dialog.time_edit, Qt.Key.Key_Backtab)
    assert dialog.focusWidget() is dialog.date_edit
    assert dialog.date_edit.text().endswith(
        f"({('월', '화', '수', '목', '금', '토', '일')[dialog.date_edit.date().dayOfWeek() - 1]})"
    )
    assert dialog.date_edit.sectionCount() == 3


@pytest.mark.parametrize("size", [(760, 560), (1100, 760)])
def test_preview_layout_and_editor_buttons_fit(live_window, qtbot, tmp_path, size):
    window, _env, task, _now, _repo = live_window
    window.resize(*size)
    qtbot.waitUntil(lambda: len(window._today_reminders._result.items) == 5, timeout=5000)
    qtbot.wait(100)
    assert window._task_content.width() == window._content_stack.width()
    assert window.width() == size[0]
    assert (
        window.minimumHeight() <= window.height() <= size[1]
    )  # Windows can constrain a tall window to the monitor.
    assert window._today_reminders.list.height() >= 32
    preview_list = window._today_reminders.list
    assert (
        preview_list.visualItemRect(preview_list.item(preview_list.count() - 1)).bottom()
        < preview_list.viewport().height()
    )
    assert window.grab().save(str(tmp_path / "today-reminders.png"))
    window._open_editor(task)
    editor = next(d for d in window._file_dialogs if isinstance(d, TaskEditorDialog))
    assert not editor.isModal()
    qtbot.wait(50)
    assert editor.rect().contains(editor.save_button.geometry())
    assert (
        editor.reminder_settings_button.width()
        >= editor.reminder_settings_button.sizeHint().width()
    )
    assert editor.grab().save(str(tmp_path / "editor-reminders.png"))
    editor._open_reminder_settings()
    settings = editor._reminder_settings
    assert settings is not None
    qtbot.wait(50)
    assert settings.grab().save(str(tmp_path / "settings-reminders.png"))
    editor.reject()


def test_weekday_timezone_and_boundary():
    assert format_date(date(2026, 10, 6)) == "2026-10-06(화)"
    assert format_date(date(2028, 2, 29), short=True) == "02.29(화)"
    assert (
        format_datetime(datetime(2026, 12, 31, 16, 0, tzinfo=UTC), "Asia/Seoul")
        == "2027-01-01(금) 01:00"
    )


def test_unscheduled_task_keeps_absolute_reminders(qtbot):
    rule = Rule(ReminderRelation.ABSOLUTE, absolute_at=datetime.now(UTC) + timedelta(days=3))
    editor = TaskEditorDialog(
        timezone="Asia/Seoul", task=Task.create(title="일정 없음"), reminder_rules=(rule,)
    )
    qtbot.addWidget(editor)
    editor._validate_and_accept()
    assert editor.result() == QDialog.DialogCode.Accepted
    assert editor.draft().starts_at is None
    assert editor.draft().reminder_rules == (rule,)


def test_alarm_rows_indicate_attachments_and_same_editor_is_reused(live_window, qtbot):
    window, env, task, _now, _repo = live_window
    env.add(task.id, "제출자료.txt")
    window._refresh_tasks()
    qtbot.waitUntil(
        lambda: (
            len(window._today_reminders._result.items) == 5
            and window._today_reminders._result.items[0].task.has_attachments
        ),
        timeout=5000,
    )
    window._open_editor(task)
    editor = next(d for d in window._file_dialogs if isinstance(d, TaskEditorDialog))
    editor.title_edit.setText("작성 중 내용")
    window._open_editor(env.tasks.get(task.id))
    assert sum(isinstance(d, TaskEditorDialog) for d in window._file_dialogs) == 1
    assert editor.title_edit.text() == "작성 중 내용"


def test_runtime_refresh_keeps_all_open_drafts_and_conflict_baseline(live_window, qtbot):
    window, env, task, now, _repo = live_window
    window._open_editor(task)
    editor = next(d for d in window._file_dialogs if isinstance(d, TaskEditorDialog))
    editor.title_edit.setText("보존할 제목")
    editor.description_edit.setPlainText("아직 저장하지 않은 설명")
    editor._open_reminder_settings()
    settings = editor._reminder_settings
    settings.list.setCurrentRow(2)
    settings._open_rule(True)
    child = settings._child
    child.time_edit.setTime(child.time_edit.time().addSecs(600))
    baseline = editor._task
    edits = settings._edits.copy()
    child_time = child.time_edit.time()
    env.tasks.complete(task.id, now=now)
    window._synchronize_reminder_dialog()
    assert editor._task == baseline
    assert editor.title_edit.text() == "보존할 제목"
    assert editor.description_edit.toPlainText() == "아직 저장하지 않은 설명"
    assert editor.status_combo.currentData() == "active"
    assert settings._edits == edits
    assert settings.list.currentRow() == 2
    assert child.time_edit.time() == child_time
    assert "업무 완료" in settings.runtime_label.text()
    assert "이후 알림 중단" in editor.reminder_summary.text()
    child.reject()
    settings.reject()
    qtbot.mouseClick(editor.save_button, Qt.MouseButton.LeftButton)
    assert editor.isVisible()
    assert env.tasks.get(task.id).status is TaskStatus.COMPLETED
    assert editor.title_edit.text() == "보존할 제목"


def test_last_export_directory_updates_already_open_controllers(live_window, tmp_path):
    from officeflow.application.attachment_export import AttachmentExportService
    from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
    from officeflow.presentation.attachment_export import AttachmentExportController

    window, env, _task, _now, _repo = live_window
    service = AttachmentExportService(env.attachments, ExternalExportStorage(tmp_path / "data"))
    first = AttachmentExportController(service, window, initial_directory=str(tmp_path / "old"))
    second = AttachmentExportController(service, window, initial_directory=None)
    directory = str(tmp_path / "latest")
    window._remember_attachment_export_directory(directory)
    assert first.directory == second.directory == directory
    assert window._settings.attachment_export_directory == directory


def test_actual_window_click_remains_available_over_modeless_editors(live_window, qtbot):
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest

    from officeflow.domain.enums import ReminderDeliveryStatus

    window, _env, task, now, repo = live_window
    window._open_editor(task)
    editor = next(d for d in window._file_dialogs if isinstance(d, TaskEditorDialog))
    editor.title_edit.setText("입력 내용 보존")
    editor._open_reminder_settings()
    settings = editor._reminder_settings
    settings._open_rule(False)
    alerts = window._reminder_service.poll_due(now=now)
    window._present_reminder_alerts(alerts)
    alert = window._reminder_dialog
    qtbot.wait(80)
    position = alert.acknowledge_button.mapTo(alert, alert.acknowledge_button.rect().center())
    QTest.mouseClick(
        alert.windowHandle(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        QPoint(position),
    )
    qtbot.waitUntil(lambda: window._reminder_dialog is None)
    assert editor.isVisible() and editor.title_edit.text() == "입력 내용 보존"
    assert settings._child.isVisible()
    assert (
        repo.get_reminder_delivery(alerts[0].delivery.id).status
        is ReminderDeliveryStatus.ACKNOWLEDGED
    )
    assert editor._deliveries[0].status is ReminderDeliveryStatus.ACKNOWLEDGED
    assert "확인함" in settings.list.item(0).text()


def test_settings_saved_via_ui_create_multiple_rules(live_window, qtbot):
    window, _env, task, now, repo = live_window
    window._open_editor(task)
    editor = next(d for d in window._file_dialogs if isinstance(d, TaskEditorDialog))
    editor._open_reminder_settings()
    settings = editor._reminder_settings
    settings._open_rule(False)
    child = settings._child
    new_time = now + timedelta(days=10)
    from zoneinfo import ZoneInfo

    local = new_time.astimezone(ZoneInfo("Asia/Seoul"))
    from PySide6.QtCore import QDate, QTime

    child.date_edit.setDate(QDate(local.year, local.month, local.day))
    child.time_edit.setTime(QTime(local.hour, local.minute))
    child._accept_rule()
    settings._apply()
    qtbot.mouseClick(editor.save_button, Qt.MouseButton.LeftButton)
    assert len(repo.list_reminders(task.id)) == 6
    assert not editor.isVisible()


def test_open_reminder_settings_follow_end_repeat_and_status_changes(qtbot):
    start = (datetime.now(UTC) + timedelta(days=20)).replace(
        hour=1, minute=0, second=0, microsecond=0
    )
    task = Task.create(title="기준 변경", starts_at=start, ends_at=start + timedelta(hours=1))
    editor = TaskEditorDialog(
        timezone="Asia/Seoul",
        task=task,
        reminder_rules=(Rule(ReminderRelation.END, offset_minutes=0),),
    )
    qtbot.addWidget(editor)
    editor.show()
    editor._open_reminder_settings()
    settings = editor._reminder_settings
    settings._open_rule(True)
    child = settings._child
    editor.end_enabled_check.setChecked(False)
    assert settings._task.ends_at is None
    assert "기준 시각이 없습니다" in child.preview.text()
    editor.end_enabled_check.setChecked(True)
    editor.repeat_combo.setCurrentIndex(editor.repeat_combo.findData("DAILY"))
    assert settings._task.recurrence_rule == "FREQ=DAILY;INTERVAL=1"
    editor.repeat_interval.setValue(2)
    assert "INTERVAL=2" in child._task.recurrence_rule
    editor.status_combo.setCurrentIndex(editor.status_combo.findData("completed"))
    assert "업무 완료 · 이후 알림 중단" in editor.reminder_summary.text()


def test_calendar_alarm_count_selects_and_scrolls_to_pending_alarm(live_window, qtbot):
    from zoneinfo import ZoneInfo

    window, env, _task, now, _repo = live_window
    for number in range(8):
        # The day list sorts tasks and reminder groups together by effective time.
        # Earlier tasks keep the target alarm below the fold so scrolling is tested.
        env.tasks.create(TaskDraft(f"오늘 업무 {number}", starts_at=now - timedelta(minutes=10)))
    window._show_calendar()
    page = window._calendar_page
    day = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    page._set_month_and_date(day)
    qtbot.waitUntil(lambda: page.day_list.count() == 9, timeout=5000)
    page.calendar.alarmRequested.emit(day)
    qtbot.waitUntil(
        lambda: isinstance(
            page.day_list.currentItem().data(Qt.ItemDataRole.UserRole), ReminderGroup
        ),
        timeout=5000,
    )
    qtbot.wait(80)
    assert page.day_list.currentRow() == 8
    assert (
        page.day_list.viewport()
        .rect()
        .intersects(page.day_list.visualItemRect(page.day_list.currentItem()))
    )


def test_repeating_absolute_reminder_requires_whole_task_confirmation(live_window, qtbot):
    from PySide6.QtWidgets import QMessageBox

    from officeflow.domain.enums import ReminderDeliveryStatus, TaskStatus

    window, env, _task, now, repo = live_window
    task = window._task_save_service.save(
        TaskDraft("반복 전체 완료 확인", starts_at=now, recurrence_rule="FREQ=DAILY;COUNT=3"),
        edits=(ReminderEdit(Rule(ReminderRelation.ABSOLUTE, absolute_at=now)),),
        now=now,
    )
    alert = next(a for a in window._reminder_service.poll_due(now=now) if a.task.id == task.id)
    window._present_reminder_alerts((alert,))
    assert window._reminder_dialog.complete_button.text() == "업무 전체 완료…"
    window._handle_reminder_action(alert.delivery.id, "complete")
    box = next(d for d in window._file_dialogs if isinstance(d, QMessageBox))
    assert not box.isModal()
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.No), Qt.MouseButton.LeftButton)
    assert env.tasks.get(task.id).status is TaskStatus.ACTIVE
    assert repo.get_reminder_delivery(alert.delivery.id).status is ReminderDeliveryStatus.FIRED
    window._handle_reminder_action(alert.delivery.id, "complete")
    box = next(d for d in window._file_dialogs if isinstance(d, QMessageBox))
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Yes), Qt.MouseButton.LeftButton)
    assert env.tasks.get(task.id).status is TaskStatus.COMPLETED


def test_acknowledging_one_reminder_preserves_future_reminders_after_restart(live_window, qtbot):
    window, env, task, now, repo = live_window
    alert = window._reminder_service.poll_due(now=now)[0]
    original_rules = repo.list_reminders(task.id)
    window._present_reminder_alerts((alert,))
    qtbot.mouseClick(window._reminder_dialog.acknowledge_button, Qt.MouseButton.LeftButton)
    assert window._reminder_dialog is None
    assert env.tasks.get(task.id).status is TaskStatus.ACTIVE
    assert (
        repo.get_reminder_delivery(alert.delivery.id).status is ReminderDeliveryStatus.ACKNOWLEDGED
    )
    assert repo.list_reminders(task.id) == original_rules
    restarted = ReminderService(repo, env.tasks)
    assert restarted.poll_due(now=now, recover_unhandled=True) == ()
    next_alerts = restarted.poll_due(now=now + timedelta(minutes=1))
    assert len(next_alerts) == 1
    assert next_alerts[0].task.id == task.id
    assert next_alerts[0].reminder.id != alert.reminder.id


def test_nonrecurring_completion_cancels_only_after_confirmation(live_window, qtbot):
    window, env, task, now, repo = live_window
    first = window._reminder_service.poll_due(now=now)[0]
    snoozed = window._reminder_service.snooze(first.delivery.id, 60, now=now)
    alert = window._reminder_service.poll_due(now=now + timedelta(minutes=1))[0]
    window._present_reminder_alerts((alert,))
    popup = window._reminder_dialog
    qtbot.mouseClick(popup.complete_button, Qt.MouseButton.LeftButton)
    box = next(d for d in window._file_dialogs if isinstance(d, QMessageBox))
    assert not box.isModal()
    assert box.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert box.parentWidget() is popup
    assert task.title in box.text()
    assert "남은 알림·재알림을 모두 중단" in box.text()
    assert "이번 알림 확인" in box.informativeText()
    assert box.defaultButton() is box.button(QMessageBox.StandardButton.No)
    window._handle_reminder_action(alert.delivery.id, "complete")
    assert sum(isinstance(d, QMessageBox) for d in window._file_dialogs) == 1
    qtbot.wait(80)
    qtbot.keyClick(box, Qt.Key.Key_Return)
    assert not box.isVisible()
    assert env.tasks.get(task.id).status is TaskStatus.ACTIVE
    assert repo.get_reminder_delivery(alert.delivery.id).status is ReminderDeliveryStatus.FIRED
    assert repo.get_reminder_delivery(snoozed.id).status is ReminderDeliveryStatus.SNOOZED
    qtbot.mouseClick(popup.complete_button, Qt.MouseButton.LeftButton)
    box = next(d for d in window._file_dialogs if isinstance(d, QMessageBox))
    from PySide6.QtTest import QTest

    button = box.button(QMessageBox.StandardButton.Yes)
    position = button.mapTo(box, button.rect().center())
    qtbot.wait(80)
    QTest.mouseClick(
        box.windowHandle(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position
    )
    assert env.tasks.get(task.id).status is TaskStatus.COMPLETED
    assert window._reminder_dialog is None
    assert repo.get_reminder_delivery(snoozed.id).status is ReminderDeliveryStatus.COMPLETED
    assert window._reminder_service.poll_due(now=now + timedelta(minutes=60)) == ()


def test_occurrence_completion_preserves_next_occurrence_alerts(live_window, qtbot):
    window, env, _task, now, _repo = live_window
    task = window._task_save_service.save(
        TaskDraft("회차만 완료", starts_at=now, recurrence_rule="FREQ=DAILY;COUNT=3"),
        edits=(
            ReminderEdit(Rule(ReminderRelation.START, offset_minutes=0)),
            ReminderEdit(Rule(ReminderRelation.START, offset_minutes=1)),
        ),
        now=now,
    )
    alert = next(a for a in window._reminder_service.poll_due(now=now) if a.task.id == task.id)
    window._present_reminder_alerts((alert,))
    assert window._reminder_dialog.complete_button.text() == "이번 회차 완료…"
    qtbot.mouseClick(window._reminder_dialog.complete_button, Qt.MouseButton.LeftButton)
    box = next(d for d in window._file_dialogs if isinstance(d, QMessageBox))
    assert "다음 반복 회차의 알림은 유지" in box.text()
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Yes), Qt.MouseButton.LeftButton)
    assert env.tasks.get(task.id).status is TaskStatus.ACTIVE
    assert not any(
        a.task.id == task.id
        for a in window._reminder_service.poll_due(now=now + timedelta(minutes=1))
    )
    next_alerts = window._reminder_service.poll_due(now=now + timedelta(days=1))
    assert any(
        a.task.id == task.id and a.occurrence_start == now + timedelta(days=1) for a in next_alerts
    )


def test_defer_is_not_a_future_reminder_mute(live_window, qtbot):
    window, env, task, now, repo = live_window
    alert = window._reminder_service.poll_due(now=now)[0]
    window._present_reminder_alerts((alert,))
    qtbot.mouseClick(window._reminder_dialog.defer_button, Qt.MouseButton.LeftButton)
    assert env.tasks.get(task.id).status is TaskStatus.PENDING
    assert repo.get_reminder_delivery(alert.delivery.id).status is ReminderDeliveryStatus.DEFERRED
    assert len(window._reminder_service.poll_due(now=now + timedelta(minutes=1))) == 1


def test_confirmation_cannot_silently_change_completion_scope(live_window, qtbot):
    window, env, task, now, repo = live_window
    alert = window._reminder_service.poll_due(now=now)[0]
    window._present_reminder_alerts((alert,))
    qtbot.mouseClick(window._reminder_dialog.complete_button, Qt.MouseButton.LeftButton)
    box = next(d for d in window._file_dialogs if isinstance(d, QMessageBox))
    env.tasks.update(
        task.id,
        TaskDraft(task.title, starts_at=task.starts_at, recurrence_rule="FREQ=DAILY;COUNT=3"),
        now=now,
    )
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Yes), Qt.MouseButton.LeftButton)
    assert env.tasks.get(task.id).status is TaskStatus.ACTIVE
    assert repo.get_reminder_delivery(alert.delivery.id).status is ReminderDeliveryStatus.FIRED
    assert "반복 범위가 변경" in window.statusBar().currentMessage()


def test_confirmation_is_canceled_if_alert_is_acknowledged_elsewhere(live_window, qtbot):
    window, env, task, now, repo = live_window
    alert = window._reminder_service.poll_due(now=now)[0]
    window._present_reminder_alerts((alert,))
    qtbot.mouseClick(window._reminder_dialog.complete_button, Qt.MouseButton.LeftButton)
    box = next(d for d in window._file_dialogs if isinstance(d, QMessageBox))
    window._handle_reminder_action(alert.delivery.id, "acknowledge")
    assert window._reminder_dialog is None
    assert not box.isVisible()
    assert env.tasks.get(task.id).status is TaskStatus.ACTIVE
    assert (
        repo.get_reminder_delivery(alert.delivery.id).status is ReminderDeliveryStatus.ACKNOWLEDGED
    )
    qtbot.wait(30)
    next_alerts = window._reminder_service.poll_due(now=now + timedelta(minutes=1))
    assert len(next_alerts) == 1


def test_acknowledging_another_reminder_keeps_pending_snooze(live_window, qtbot, monkeypatch):
    window, env, task, now, repo = live_window
    snooze = window._reminder_service.snooze
    monkeypatch.setattr(
        window._reminder_service,
        "snooze",
        lambda delivery_id, minutes: snooze(delivery_id, minutes, now=now),
    )
    first = window._reminder_service.poll_due(now=now)[0]
    window._present_reminder_alerts((first,))
    qtbot.mouseClick(window._reminder_dialog.snooze_button, Qt.MouseButton.LeftButton)
    snoozed = repo.get_reminder_delivery(first.delivery.id)
    second = window._reminder_service.poll_due(now=now + timedelta(minutes=1))[0]
    window._present_reminder_alerts((second,))
    qtbot.mouseClick(window._reminder_dialog.acknowledge_button, Qt.MouseButton.LeftButton)
    assert env.tasks.get(task.id).status is TaskStatus.ACTIVE
    assert repo.get_reminder_delivery(first.delivery.id) == snoozed
    assert (
        repo.get_reminder_delivery(second.delivery.id).status is ReminderDeliveryStatus.ACKNOWLEDGED
    )
    restarted = ReminderService(repo, env.tasks)
    alerts = restarted.poll_due(now=snoozed.snoozed_until)
    assert any(a.delivery.id == first.delivery.id for a in alerts)


@pytest.mark.parametrize("size", [(420, 340), (520, 420)])
def test_reminder_primary_and_secondary_actions_fit_small_windows(
    live_window, qtbot, tmp_path, size
):
    window, _env, _task, now, _repo = live_window
    alert = window._reminder_service.poll_due(now=now)[0]
    window._present_reminder_alerts((alert,))
    popup = window._reminder_dialog
    popup.resize(*size)
    qtbot.wait(80)
    for button in (
        popup.acknowledge_button,
        popup.complete_button,
        popup.defer_button,
        popup.snooze_button,
    ):
        from PySide6.QtCore import QRect

        assert popup.rect().contains(
            QRect(button.mapTo(popup, button.rect().topLeft()), button.size())
        )
        assert button.width() >= button.sizeHint().width()
    assert popup.acknowledge_button.y() < popup.complete_button.y()
    assert popup.grab().save(str(tmp_path / "reminder-action-scope.png"))


def test_shutdown_closes_today_full_reminder_window(live_window, qtbot):
    window, _env, _task, _now, _repo = live_window
    qtbot.waitUntil(lambda: len(window._today_reminders._result.items) == 5, timeout=5000)
    window._today_reminders._open_all()
    dialog = window._today_reminders._dialog
    assert dialog.isVisible()
    window.shutdown()
    assert window._today_reminders._dialog is None
    assert not dialog.isVisible()


@pytest.mark.parametrize("minutes", [-28800, -14400, 0, 75])
def test_relative_interval_units_preserve_existing_rule_and_conversion(qtbot, minutes):
    from zoneinfo import ZoneInfo

    start = (datetime.now(UTC) + timedelta(days=30)).replace(second=0, microsecond=0)
    task = Task.create(title="기준 간격", starts_at=start)
    edit = ReminderEdit(Rule(ReminderRelation.START, offset_minutes=minutes), 7)
    dialog = ReminderRuleDialog(task, edit=edit)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog._edit() == edit
    dialog.mode.setCurrentIndex(dialog.mode.findData("absolute"))
    expected = (start.astimezone(ZoneInfo(task.timezone)) + timedelta(minutes=minutes)).astimezone(
        UTC
    )
    assert dialog._edit().rule.absolute_at == expected
    assert not dialog.interval_row.isVisible()


def test_relative_quick_choice_and_custom_day_input(qtbot):
    task = Task.create(title="간격", starts_at=datetime.now(UTC) + timedelta(days=30))
    dialog = ReminderRuleDialog(task)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.mode.setCurrentIndex(dialog.mode.findData("start"))
    dialog.quick.setCurrentIndex(dialog.quick.findData(-28800))
    assert dialog.offset.value() == 20
    assert dialog.unit.currentData() == 1440
    assert dialog.direction.currentData() == -1
    assert "20일 전" in dialog.preview.text()
    dialog.offset.setValue(6)
    assert dialog.quick.currentData() is None
    assert dialog._edit().rule.offset_minutes == -8640
    dialog.direction.setCurrentIndex(dialog.direction.findData(1))
    assert dialog._edit().rule.offset_minutes == 8640
