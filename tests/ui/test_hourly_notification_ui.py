"""Presentation/integration tests for the independent clock-hour reminder."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QDialog, QMessageBox, QWidget
from pytestqt.qtbot import QtBot

from officeflow.application.reminders import ReminderService
from officeflow.application.tasks import TaskDraft, TaskService
from officeflow.domain.enums import ReminderRelation
from officeflow.domain.hourly_notification import HourlySettings
from officeflow.domain.reminder import ReminderRuleInput
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.hourly_notification_controller import HourlyNotificationController
from officeflow.presentation.main_window import MainWindow
from tests.unit.test_reminder_service import InMemoryReminderRepository
from tests.unit.test_task_service import InMemoryTaskRepository


class FakeClock:
    def __init__(self, time: str = "2026-10-01T19:40") -> None:
        self.value = datetime.fromisoformat(time).replace(tzinfo=ZoneInfo("Asia/Seoul"))
        self.seconds = 0.0

    def now(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)
        self.seconds += seconds


class FakeStartupManager:
    def set_enabled(self, _enabled: bool) -> None:
        pass


def controller(
    qtbot: QtBot, clock: FakeClock, save=lambda _value: None
) -> HourlyNotificationController:
    parent = QWidget()
    qtbot.addWidget(parent)
    result = HourlyNotificationController(
        HourlySettings(), parent=parent, save_settings=save, other_window=lambda: None, clock=clock
    )
    result._test_parent = parent  # qtbot keeps only weak references to added widgets.
    result.start()
    return result


def test_nonmodal_topmost_single_window_and_snooze(qtbot: QtBot) -> None:
    clock = FakeClock()
    control = controller(qtbot, clock)
    dialog = control.dialog
    assert dialog is not None
    assert dialog.isVisible()
    assert not dialog.isModal()
    assert dialog.parent() is None
    assert dialog.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert not dialog.dismiss_button.autoDefault()
    dialog.snooze_button.click()
    assert not dialog.isVisible()
    assert control.service.snoozed_until == clock.now() + timedelta(minutes=5)
    assert "19:45" in control.status_action.text()
    assert "20:35" in control.status_action.text()
    clock.advance(300)
    control.poll()
    assert control.dialog is dialog
    assert dialog.isVisible()
    dialog.dismiss_button.click()
    assert not dialog.isVisible()
    assert control.service.state(clock.now()).current_status == "closed"
    control.shutdown()


def test_x_and_escape_close_only_current_hour_and_stale_button_ignored(qtbot: QtBot) -> None:
    clock = FakeClock("2026-10-01T19:59:55")
    control = controller(qtbot, clock)
    dialog = control.dialog
    assert dialog is not None
    old_key = control.service.open_key
    clock.advance(5)
    dialog.dismissRequested.emit(old_key)
    assert control.service.open_key != old_key
    assert "20:00" in dialog.hour_label.text()
    dialog.reject()
    assert not dialog.isVisible()
    clock.advance(3600)
    control.poll()
    assert dialog.isVisible()
    dialog.close()
    assert control.service.state(clock.now()).current_status == "closed"
    control.shutdown()


def test_ordinary_file_window_and_settings_do_not_block_hourly(qtbot: QtBot) -> None:
    file_window = QDialog(None)
    file_window.setModal(False)
    qtbot.addWidget(file_window)
    file_window.show()
    control = controller(qtbot, FakeClock())
    control.open_settings()
    settings = control.settings_dialog
    assert settings is not None and not settings.isModal() and settings.parent() is None
    assert control.dialog is not None
    control.dialog.dismiss_button.click()
    assert file_window.isVisible()
    assert settings.isVisible()
    settings.close()
    control.shutdown()


def test_settings_save_merges_current_runtime_dates_and_failures_revert_checkbox(
    qtbot: QtBot,
) -> None:
    saved = []
    clock = FakeClock("2026-10-01T12:00")
    control = controller(qtbot, clock, saved.append)
    control.open_settings()
    editor = control.settings_dialog
    assert editor is not None
    editor.holiday_check.click()
    assert control.service.settings.holiday_date == "2026-10-01"
    editor.minute_spin.setValue(40)
    editor._save()
    assert control.service.settings.minute == 40
    assert control.service.settings.holiday_date == "2026-10-01"
    assert saved[-1].holiday_date == "2026-10-01"
    control.shutdown()

    def fail(_value: HourlySettings) -> None:
        raise OSError("disk full")

    control = controller(qtbot, clock, fail)
    control.open_settings()
    editor = control.settings_dialog
    assert editor is not None
    editor.holiday_check.click()
    assert control.service.settings.holiday_date is None
    assert not editor.holiday_check.isChecked()
    assert "적용하지 못했습니다" in editor.error_label.text()
    control.shutdown()


def test_shutdown_cancels_pending_start_and_open_windows(qtbot: QtBot) -> None:
    clock = FakeClock()
    control = controller(qtbot, clock)
    control.open_settings()
    control.shutdown()
    control.shutdown()
    control.start()
    clock.advance(3600)
    control.poll()
    assert not control.timer.isActive()
    assert control.dialog is None
    assert control.settings_dialog is None
    assert control.service.open_key is None


def test_main_window_default_test_mode_has_no_real_hourly_timer(
    qtbot: QtBot, task_service: TaskService
) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(10)
    assert not window._hourly_controller.timer.isActive()
    assert window._hourly_controller.dialog is None


def test_main_window_controls_and_modeless_editors_merge_settings(
    qtbot: QtBot, task_service: TaskService
) -> None:
    saved = []
    window = MainWindow(
        AppSettings(),
        task_service,
        save_settings=saved.append,
        startup_manager=FakeStartupManager(),
        hourly_notifications_enabled=True,
        hourly_notification_clock=FakeClock("2026-10-01T12:00"),
    )
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window._hourly_controller.timer.isActive())
    assert not window._hourly_button.isVisible()
    window._open_settings()
    general = window._settings_dialog
    assert general is not None and not general.isModal()
    general.hourly_button.click()
    hourly = window._hourly_controller.settings_dialog
    assert hourly is not None
    hourly.holiday_check.click()
    hourly.minute_spin.setValue(42)
    hourly._save()
    general.backup_keep_spin.setValue(22)
    general._validate_and_accept()
    assert window._settings.hourly_notification_holiday_date == "2026-10-01"
    assert window._settings.hourly_notification_minute == 42
    assert window._settings.automatic_backup_keep == 22
    hourly.minute_spin.setValue(45)
    hourly._save()
    assert saved[-1].automatic_backup_keep == 22
    window._hourly_controller.enabled_action.trigger()
    assert "꺼짐" in window._hourly_button.text()
    window.handle_external_command("activate")
    assert not window._hourly_controller.service.enabled
    assert window._reminder_timer.isActive() is False
    window.shutdown()
    assert not window._hourly_controller.timer.isActive()


def test_reposition_avoids_existing_task_reminder(qtbot: QtBot) -> None:
    other = QDialog(None)
    other.resize(300, 200)
    qtbot.addWidget(other)
    parent = QWidget()
    qtbot.addWidget(parent)
    control = HourlyNotificationController(
        HourlySettings(),
        parent=parent,
        save_settings=lambda _: None,
        other_window=lambda: other,
        clock=FakeClock(),
    )
    control.start()
    dialog = control.dialog
    assert dialog is not None
    other.move(dialog.pos())
    other.show()
    control.reposition()
    assert dialog.frameGeometry().intersected(other.frameGeometry()).width() < other.width()
    control.shutdown()


def test_presentation_failure_no_repeated_window_creation(qtbot: QtBot, monkeypatch) -> None:
    import officeflow.presentation.hourly_notification_controller as module

    calls = []

    def fail():
        calls.append(True)
        raise RuntimeError("platform UI failure")

    monkeypatch.setattr(module, "HourlyNotificationDialog", fail)
    clock = FakeClock()
    control = controller(qtbot, clock)
    for _ in range(5):
        clock.advance(5)
        control.poll()
    assert len(calls) == 1
    assert control.service.history_size == 0
    assert control.service.error is not None
    control.shutdown()


@pytest.fixture
def windows_test_fonts() -> Iterator[None]:
    # Offscreen Qt does not discover Windows system fonts automatically.
    fonts = [QFontDatabase.addApplicationFont(str(path)) for path in (
        Path("C:/Windows/Fonts/malgun.ttf"), Path("C:/Windows/Fonts/segoeui.ttf")
    ) if path.exists()]
    yield
    for font in fonts:
        QFontDatabase.removeApplicationFont(font)


def test_long_message_and_advanced_settings_render_with_scrolling(qtbot: QtBot, windows_test_fonts) -> None:
    control = controller(qtbot, FakeClock())
    control.service.update_settings(
        replace(control.service.settings, message="인사랑 업무기록 확인 " * 16), control.clock.now()
    )
    control.open_settings()
    editor = control.settings_dialog
    assert editor is not None
    for _ in range(8):
        editor.add_range_button.click()
    assert len(editor._ranges) == 8
    assert not editor.add_range_button.isEnabled()
    editor._save()
    assert control.service.settings.extra_exclusions == (("12:00", "13:00"),) * 8
    control.poll()
    assert control.dialog is not None
    qtbot.wait(10)
    output = Path("artifacts/hourly-ui")
    output.mkdir(parents=True, exist_ok=True)
    assert control.dialog.grab().save(str(output / "notification.png"))
    assert editor.grab().save(str(output / "settings.png"))
    editor.advanced_toggle.click()
    qtbot.wait(10)
    assert editor.grab().save(str(output / "settings-advanced.png"))
    control.shutdown()


def test_hourly_off_does_not_disable_task_reminders_or_modify_tasks(qtbot: QtBot) -> None:
    tasks = InMemoryTaskRepository()
    service = TaskService(tasks)
    reminders = InMemoryReminderRepository(tasks)
    reminder_service = ReminderService(reminders, service)
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    task = service.create(TaskDraft(title="업무 알림", starts_at=now - timedelta(minutes=1)))
    reminder_service.replace_rules(task.id, (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),))
    window = MainWindow(AppSettings(), service, reminder_service=reminder_service,
                        hourly_notifications_enabled=True, hourly_notification_clock=FakeClock())
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window._hourly_controller.dialog is not None and window._reminder_dialog is not None)
    before = (dict(tasks.tasks), dict(reminders.reminders), dict(reminders.deliveries), len(tasks.queries))
    window._hourly_controller.enabled_action.trigger()
    for _ in range(5):
        window._hourly_controller.poll()
    assert window._reminder_dialog.isVisible()
    assert window._reminder_timer.isActive()
    assert before == (tasks.tasks, reminders.reminders, reminders.deliveries, len(tasks.queries))
    assert tasks.update_calls == 0
    window._reminder_dialog.acknowledge_button.click()
    assert not window._hourly_controller.service.enabled
    window.shutdown()


def test_unclean_warning_before_hourly_and_shutdown_before_pending_start(qtbot: QtBot, task_service: TaskService, monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, *_args: seen.append(parent._hourly_controller.dialog is None))
    window = MainWindow(AppSettings(), task_service, previous_unclean_shutdown=True,
                        hourly_notifications_enabled=True, hourly_notification_clock=FakeClock())
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window._hourly_controller.dialog is not None)
    assert seen == [True]
    window.shutdown()
    pending = MainWindow(AppSettings(), task_service, hourly_notifications_enabled=True,
                         hourly_notification_clock=FakeClock())
    qtbot.addWidget(pending)
    pending.shutdown()
    qtbot.wait(10)
    assert pending._hourly_controller.dialog is None


def test_small_main_status_does_not_overlap_undo_and_close_survives_settings_error(qtbot: QtBot, task_service: TaskService) -> None:
    calls = []
    def fail(_settings: AppSettings) -> None:
        raise OSError("disk full")
    window = MainWindow(AppSettings(), task_service, save_settings=fail,
                        on_shutdown=lambda: calls.append(True), hourly_notifications_enabled=True,
                        hourly_notification_clock=FakeClock())
    qtbot.addWidget(window)
    window.resize(760, 560)
    window.show()
    qtbot.waitUntil(lambda: window._hourly_button.isVisible())
    window._undo_button.show()
    qtbot.wait(10)
    assert not window._hourly_button.geometry().intersects(window._undo_button.geometry())
    assert window._hourly_button.geometry().right() <= window.statusBar().width()
    window.close()
    assert calls == [True]
    assert not window._hourly_controller.timer.isActive()


@pytest.mark.parametrize("closed", [True, False])
def test_compact_status_shows_next_time_without_clipped_hour_action(
    qtbot: QtBot, task_service: TaskService, windows_test_fonts, closed: bool
) -> None:
    clock = FakeClock()
    window = MainWindow(AppSettings(), task_service, hourly_notifications_enabled=True,
                        hourly_notification_clock=clock)
    qtbot.addWidget(window)
    window.resize(760, 560)
    window.show()
    control = window._hourly_controller
    qtbot.waitUntil(lambda: control.dialog is not None)
    assert control.dialog is not None
    assert control.dialog.dismiss_button.text() == "닫기"
    assert control.dialog.snooze_button.text() == "5분 뒤 알림"
    assert control.dialog.height() < 275
    assert control.dialog.dismiss_button.width() >= control.dialog.dismiss_button.sizeHint().width()
    if closed:
        control.dialog.dismiss_button.click()
    else:
        control.skip_action.trigger()
    qtbot.wait(10)
    assert window._hourly_button.text() == "다음 20:35"
    assert "이번 시간" not in window._hourly_button.text()
    assert "이번 시간" in window._hourly_button.toolTip()
    assert window._hourly_button.width() >= window._hourly_button.sizeHint().width()
    output = Path("artifacts/hourly-ui")
    output.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(output / "compact-status.png"))
    control.clock.advance(3600)
    control.poll()
    assert control.dialog.grab().save(str(output / "compact-notification.png"))
    window.shutdown()


def test_morning_setting_positive_wording_keeps_default_rule_and_roundtrips(qtbot: QtBot) -> None:
    clock = FakeClock("2026-10-01T08:30")
    control = controller(qtbot, clock)
    control.open_settings()
    editor = control.settings_dialog
    assert editor is not None
    assert editor.morning_check.text() == "늦게 켜도 08시 알림 받기"
    assert not editor.morning_check.isChecked()
    assert "08시 전부터" in editor.schedule_explanation.text()
    assert editor.morning_button.text() == "오늘 08시 알림 받기"
    assert editor.morning_resume_hint.isVisible()
    editor._save()
    assert control.service.settings.morning_continuation_required
    editor.morning_check.setChecked(True)
    assert "처음 켜도" in editor.schedule_explanation.text()
    editor._save()
    assert not control.service.settings.morning_continuation_required
    editor.close()
    control.open_settings()
    assert control.settings_dialog is not None
    assert control.settings_dialog.morning_check.isChecked()
    assert not control.settings_dialog.morning_resume_hint.isVisible()
    control.shutdown()
