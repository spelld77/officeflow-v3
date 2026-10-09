from __future__ import annotations

from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.reminders import ReminderAlert
from officeflow.domain.enums import ReminderRelation
from officeflow.presentation.dates import format_datetime


class ReminderDialog(QDialog):
    actionRequested = Signal(int, str)
    snoozeRequested = Signal(int, int)

    def __init__(
        self,
        alerts: tuple[ReminderAlert, ...],
        *,
        timezone: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._timezone = ZoneInfo(timezone)
        self._alerts: dict[int, ReminderAlert] = {}

        self.setWindowTitle("OfficeFlow 알림")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(520, 420)
        self.setMinimumSize(420, 340)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        title = QLabel("확인할 알림")
        title.setObjectName("pageTitle")
        root.addWidget(title)

        self.caption = QLabel()
        self.caption.setObjectName("mutedText")
        self.caption.setWordWrap(True)
        root.addWidget(self.caption)

        self.alert_list = QListWidget()
        self.alert_list.setObjectName("reminderList")
        self.alert_list.currentItemChanged.connect(self._update_detail)
        root.addWidget(self.alert_list, 1)

        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setObjectName("mutedText")
        root.addWidget(self.detail)

        actions = QHBoxLayout()
        self.complete_button = self._action_button("업무 완료…", "complete")
        self.defer_button = self._action_button("업무 대기로", "defer")
        self.defer_button.setToolTip("업무 상태만 대기로 바꿉니다. 다음 예약 알림은 계속 울립니다.")
        self.snooze_minutes = QSpinBox()
        self.snooze_minutes.setObjectName("snoozeMinutes")
        self.snooze_minutes.setRange(1, 1_440)
        self.snooze_minutes.setValue(10)
        self.snooze_minutes.setSuffix("분")
        self.snooze_minutes.setMaximumWidth(100)
        self.snooze_minutes.setToolTip("1분에서 24시간 사이로 지정")
        self.snooze_button = QPushButton("다시 알림")
        self.snooze_button.setObjectName("snoozeButton")
        self.snooze_button.setAutoDefault(False)
        self.snooze_button.setToolTip("이번 알림만 미룹니다. 다른 예약 알림은 그대로 유지됩니다.")
        self.snooze_button.clicked.connect(self._emit_snooze)
        self.acknowledge_button = self._action_button("이번 알림 확인", "acknowledge")
        self.acknowledge_button.setObjectName("primaryButton")
        self.acknowledge_button.setDefault(True)
        self.acknowledge_button.setToolTip(
            "이번 알림만 닫습니다. 업무 상태와 다른 알림은 바뀌지 않습니다."
        )
        actions.addWidget(self.acknowledge_button, 1)
        actions.addWidget(self.snooze_minutes)
        actions.addWidget(self.snooze_button, 1)
        root.addLayout(actions)
        task_actions = QHBoxLayout()
        task_actions.addWidget(self.complete_button)
        task_actions.addWidget(self.defer_button)
        task_actions.addStretch()
        root.addLayout(task_actions)

        self.add_alerts(alerts)
        if self.alert_list.count():
            self.alert_list.setCurrentRow(0)

    def present(self) -> None:
        self.show()
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            frame = self.frameGeometry()
            frame.moveCenter(screen.availableGeometry().center())
            self.move(frame.topLeft())
        self.raise_()
        self.activateWindow()

    def dismiss_for_shutdown(self) -> None:
        self._alerts.clear()
        super().reject()

    def reject(self) -> None:
        if self._alerts:
            QApplication.beep()
            self.present()
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._alerts:
            event.ignore()
            self.present()
            return
        super().closeEvent(event)

    def add_alerts(self, alerts: tuple[ReminderAlert, ...]) -> None:
        recovered = 0
        for alert in alerts:
            delivery_id = alert.delivery.id
            if delivery_id is None or delivery_id in self._alerts:
                continue
            self._alerts[delivery_id] = alert
            recovered += int(alert.recovered)
            relation = {
                ReminderRelation.START: "시작",
                ReminderRelation.END: "종료",
                ReminderRelation.ABSOLUTE: "지정",
            }[alert.reminder.relation]
            scheduled = format_datetime(
                alert.delivery.snoozed_until or alert.delivery.scheduled_at,
                self._timezone.key,
                short=True,
            )
            prefix = "놓친 알림 · " if alert.recovered else ""
            item = QListWidgetItem(f"{prefix}{alert.task.title} · {relation} {scheduled}")
            item.setData(Qt.ItemDataRole.UserRole, delivery_id)
            self.alert_list.addItem(item)
        if recovered:
            self.caption.setText(
                f"놓친 알림 {recovered}개를 복구했습니다. 이번 알림만 확인하면 다음 예약은 유지됩니다."
            )
        elif not self.caption.text():
            self.caption.setText(
                "이번 알림만 확인하면 다음 예약은 유지됩니다. 업무 완료와 구분해 주세요."
            )
        if self.alert_list.currentRow() < 0 and self.alert_list.count():
            self.alert_list.setCurrentRow(0)

    def remove_delivery(self, delivery_id: int) -> None:
        self._alerts.pop(delivery_id, None)
        for row in range(self.alert_list.count()):
            item = self.alert_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == delivery_id:
                self.alert_list.takeItem(row)
                break
        if self.alert_list.count() == 0:
            self.accept()
        elif self.alert_list.currentRow() < 0:
            self.alert_list.setCurrentRow(0)

    def _action_button(self, label: str, action: str) -> QPushButton:
        button = QPushButton(label)
        button.setAutoDefault(False)
        button.clicked.connect(lambda _checked=False: self._emit_action(action))
        return button

    def _emit_action(self, action: str) -> None:
        item = self.alert_list.currentItem()
        if item is None:
            return
        self.actionRequested.emit(int(item.data(Qt.ItemDataRole.UserRole)), action)

    def _emit_snooze(self) -> None:
        item = self.alert_list.currentItem()
        if item is None:
            return
        self.snoozeRequested.emit(
            int(item.data(Qt.ItemDataRole.UserRole)),
            self.snooze_minutes.value(),
        )

    def _update_detail(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        if current is None:
            self.detail.clear()
            return
        alert = self._alerts.get(int(current.data(Qt.ItemDataRole.UserRole)))
        if alert is None:
            self.detail.clear()
            return
        occurrence_only = bool(alert.task.recurrence_rule and alert.occurrence_start)
        self.complete_button.setText(
            "이번 회차 완료…"
            if occurrence_only
            else "업무 전체 완료…"
            if alert.task.recurrence_rule
            else "업무 완료…"
        )
        self.complete_button.setToolTip(
            "이번 반복 회차를 완료하고 이 회차의 남은 알림을 중단합니다. 다음 회차는 유지됩니다."
            if occurrence_only
            else "업무 자체를 완료하고 이 업무의 남은 알림과 재알림을 모두 중단합니다."
        )
        schedule = format_datetime(alert.delivery.scheduled_at, self._timezone.key)
        description = alert.task.description or "설명이 없습니다."
        self.detail.setText(f"예정 알림: {schedule}\n{description}")
