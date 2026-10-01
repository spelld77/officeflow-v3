from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from officeflow.application.hourly_notifications import HourlyNotificationService, HourlyState
from officeflow.domain.hourly_notification import HourlySettings
from officeflow.presentation.hourly_notification_dialog import HourlyNotificationDialog, format_next
from officeflow.presentation.hourly_notification_settings import HourlyNotificationSettingsDialog
from officeflow.presentation.notification_coordinator import NotificationCoordinator

logger = logging.getLogger(__name__)


class NotificationClock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...


class SystemNotificationClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class HourlyNotificationController(QObject):
    stateChanged = Signal(object)

    def __init__(
        self,
        settings: HourlySettings,
        *,
        parent: QWidget,
        save_settings: Callable[[HourlySettings], None],
        other_window: Callable[[], QWidget | None],
        clock: NotificationClock | None = None,
    ) -> None:
        super().__init__(parent)
        self.clock = clock or SystemNotificationClock()
        self.service = HourlyNotificationService(settings, save_settings)
        self._other_window = other_window
        self._started = False
        self._stopped = False
        self.dialog: HourlyNotificationDialog | None = None
        self.settings_dialog: HourlyNotificationSettingsDialog | None = None
        self.timer = QTimer(self)
        self.timer.setInterval(5_000)
        self.timer.timeout.connect(self.poll)
        self.menu = QMenu("매시 알림", parent)
        self.status_action = self.menu.addAction("매시 알림 준비 중")
        self.status_action.setEnabled(False)
        self.skip_action = self.menu.addAction("이번 시간 건너뛰기")
        self.skip_action.triggered.connect(self._toggle_skip)
        self.enabled_action = self.menu.addAction("이번 실행 동안 끄기")
        self.enabled_action.triggered.connect(self._toggle_enabled)
        self.holiday_action = self.menu.addAction("오늘 휴일근무")
        self.holiday_action.setCheckable(True)
        self.holiday_action.triggered.connect(self._set_holiday)
        self.morning_action = self.menu.addAction("오늘 08시 알림 받기")
        self.morning_action.triggered.connect(self._resume_morning)
        self.menu.addAction("알림 설정...", self.open_settings)

    def start(self) -> None:
        if self._started or self._stopped:
            return
        self._started = True
        try:
            self.service.start(self.clock.now(), self.clock.monotonic())
            self.timer.start()
            self._render()
        except Exception:
            logger.exception("매시 알림 초기화에 실패했습니다.")
            self.timer.stop()
            self.service.error = "매시 알림을 시작하지 못했습니다. 프로그램을 다시 실행해 주세요."
            self.stateChanged.emit(self.service.state(self.clock.now()))

    def poll(self) -> None:
        if self._stopped or not self._started:
            return
        try:
            self.service.tick(self.clock.now(), self.clock.monotonic())
            self._render()
        except Exception:
            logger.exception("매시 알림 검사에 실패했습니다.")
            # A programming/platform failure must not flood logs every 5 seconds.
            self.timer.stop()
            self.service.error = (
                "매시 알림 검사 중 오류가 발생했습니다. 프로그램을 다시 실행해 주세요."
            )
            self.stateChanged.emit(self.service.state(self.clock.now()))

    def _render(self) -> None:
        if self._stopped:
            return
        now = self.clock.now()
        key = self.service.request_key
        if key is not None:
            try:
                if self.dialog is None:
                    self.dialog = HourlyNotificationDialog()
                    self.dialog.dismissRequested.connect(
                        lambda hour: self._act(lambda: self.service.dismiss(hour, self.clock.now()))
                    )
                    self.dialog.snoozeRequested.connect(
                        lambda hour: self._act(lambda: self.service.snooze(hour, self.clock.now()))
                    )
                    self.dialog.disableRequested.connect(
                        lambda: self._act(
                            lambda: self.service.set_session_enabled(False, self.clock.now())
                        )
                    )
                    self.dialog.settingsRequested.connect(self.open_settings)
                sound = self.service.request_sound and self.service.settings.sound
                was_visible = self.dialog.isVisible()
                # Show first, then acknowledge: failed UI creation is never a delivery.
                provisional = replace(self.service.state(now), open_key=key)
                self.dialog.update_state(provisional)
                NotificationCoordinator.place(
                    self.dialog, self._other_window(), initial=not was_visible
                )
                self.dialog.show()
                self.service.display_succeeded(key)
                if sound:
                    QApplication.beep()
            except Exception:
                logger.exception("매시 알림창을 표시하지 못했습니다.")
                if self.dialog is not None:
                    self.dialog.hide()
                self.service.display_failed(key)
        state = self.service.state(now)
        if self.dialog is not None:
            if state.open_key is None:
                self.dialog.hide()
            else:
                self.dialog.update_state(state)
                NotificationCoordinator.place(self.dialog, self._other_window())
        summary = self.status_text(state)
        self.status_action.setText(summary)
        self.skip_action.setText(
            "건너뛰기 취소" if state.current_status == "skipped" else "이번 시간 건너뛰기"
        )
        self.skip_action.setEnabled(state.current_status != "closed")
        self.enabled_action.setText("이번 실행 동안 끄기" if state.enabled else "다시 켜기")
        self.holiday_action.setEnabled(not state.weekend)
        self.holiday_action.setChecked(state.holiday_today)
        self.morning_action.setVisible(state.can_resume_morning)
        if self.settings_dialog is not None:
            self.settings_dialog.update_runtime(state, summary)
        self.stateChanged.emit(state)

    @staticmethod
    def status_text(state: HourlyState) -> str:
        if not state.enabled:
            return "매시 알림 꺼짐 · 다음 실행에는 자동 켜짐"
        reasons = {
            "extra_exclusion": "추가 제외 시간",
            "weekday_exclusion": "평일 업무시간 제외",
            "morning_without_continuation": "08시 이후 첫 실행 · 아침 알림 생략",
        }
        next_text = format_next(state.next_regular_at, state.bucket.starts_at)
        if state.reason:
            return f"{reasons[state.reason]} · 다음 {next_text}"
        if state.snoozed_until is not None:
            return f"다시 알림 {state.snoozed_until:%H:%M} · 다음 정기 {next_text}"
        if state.open_key:
            return f"현재 안내 중 · 다음 정기 {next_text}"
        if state.current_status == "closed":
            return f"이번 시간 알림 닫힘 · 다음 {next_text}"
        if state.current_status == "skipped":
            return f"이번 시간 건너뜀 · 다음 {next_text}"
        return f"다음 정기 알림 {next_text}"

    def _act(self, action: Callable[[], object]) -> None:
        if self._stopped:
            return
        try:
            # Revalidate wall-clock boundaries before handling any queued UI action.
            if self._started:
                self.service.tick(self.clock.now(), self.clock.monotonic())
            action()
            if self.settings_dialog:
                self.settings_dialog.error_label.clear()
        except (OSError, ValueError) as error:
            logger.exception("매시 알림 설정을 적용하지 못했습니다.")
            if self.settings_dialog:
                self.settings_dialog.error_label.setText(f"적용하지 못했습니다: {error}")
            self.service.error = "설정을 저장하지 못해 변경을 적용하지 않았습니다."
        self._render()

    def _toggle_skip(self) -> None:
        def apply() -> None:
            now = self.clock.now()
            if self.service.state(now).current_status == "skipped":
                self.service.cancel_skip(now)
            else:
                self.service.skip_current(now)

        self._act(apply)

    def _toggle_enabled(self) -> None:
        self._act(
            lambda: self.service.set_session_enabled(not self.service.enabled, self.clock.now())
        )

    def _set_holiday(self, enabled: bool) -> None:
        self._act(lambda: self.service.set_today_holiday(enabled, self.clock.now()))

    def _resume_morning(self) -> None:
        self._act(lambda: self.service.resume_morning_notifications(self.clock.now()))

    def open_settings(self) -> None:
        if self._stopped:
            return
        if self.settings_dialog is None:
            self.settings_dialog = HourlyNotificationSettingsDialog(self.service.settings)
            self.settings_dialog.saveRequested.connect(self._save_from_dialog)
            self.settings_dialog.holidayRequested.connect(self._set_holiday)
            self.settings_dialog.morningRequested.connect(self._resume_morning)
            self.settings_dialog.finished.connect(self._clear_settings_dialog)
        self._render()
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def _clear_settings_dialog(self, _result: int) -> None:
        if self.settings_dialog is not None:
            self.settings_dialog.deleteLater()
            self.settings_dialog = None

    def _save_from_dialog(self, value: HourlySettings) -> None:
        # Date flags may have changed while this modeless editor was open.
        updated = replace(
            value,
            timezone=self.service.settings.timezone,
            holiday_date=self.service.settings.holiday_date,
            morning_continuation_date=self.service.settings.morning_continuation_date,
        )
        self._act(lambda: self.service.update_settings(updated, self.clock.now()))
        if self.settings_dialog and self.service.settings == updated:
            self.settings_dialog.error_label.setText("설정을 저장했습니다.")

    def reposition(self) -> None:
        if self.dialog is not None and self.dialog.isVisible():
            NotificationCoordinator.place(self.dialog, self._other_window())

    def shutdown(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.timer.stop()
        self.service.shutdown()
        if self.dialog is not None:
            self.dialog.shutdown()
            self.dialog.deleteLater()
            self.dialog = None
        if self.settings_dialog is not None:
            self.settings_dialog.close()
