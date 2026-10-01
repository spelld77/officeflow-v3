from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
)

from officeflow.application.hourly_notifications import HourlyState
from officeflow.presentation.app_icon import create_app_icon
from officeflow.presentation.theme import LIGHT_STYLESHEET


def format_next(value: datetime | None, current: datetime) -> str:
    if value is None:
        return "현재 설정에서는 다음 48시간 알림 없음"
    if value.date() == current.date():
        return value.strftime("%H:%M")
    if value.date() == current.date() + timedelta(days=1):
        return f"내일 {value:%H:%M}"
    return value.strftime("%m/%d %H:%M")


class HourlyNotificationDialog(QDialog):
    dismissRequested = Signal(str)
    snoozeRequested = Signal(str)
    disableRequested = Signal()
    settingsRequested = Signal()

    def __init__(self) -> None:
        super().__init__(None)
        self._key = ""
        self._closing = False
        self._content_signature: tuple[str, bool] | None = None
        self.setWindowTitle("OfficeFlow · 매시 알림")
        self.setWindowIcon(create_app_icon())
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setModal(False)
        self.setStyleSheet(LIGHT_STYLESHEET)
        self.resize(420, 220)
        self.setMinimumWidth(360)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        self.message_label = QLabel()
        self.message_label.setTextFormat(Qt.TextFormat.PlainText)
        self.message_label.setWordWrap(True)
        self.message_label.setObjectName("sectionTitle")
        root.addWidget(self.message_label)
        self.hour_label = QLabel()
        root.addWidget(self.hour_label)
        caption = QLabel("기록은 인사랑에서 직접 해주세요.")
        caption.setWordWrap(True)
        caption.setObjectName("mutedText")
        root.addWidget(caption)
        self.early_hint = QLabel("조기출근 기록은 08시 전에 해주세요.")
        self.early_hint.setToolTip("조기출근 초과근무라면 08시 전에 인사랑에서 출근과 업무기록을 직접 해주세요.")
        self.early_hint.setWordWrap(True)
        root.addWidget(self.early_hint)
        self.next_label = QLabel()
        self.next_label.setWordWrap(True)
        root.addWidget(self.next_label)
        row = QHBoxLayout()
        self.snooze_button = QPushButton("5분 뒤 알림")
        self.dismiss_button = QPushButton("닫기")
        self.dismiss_button.setToolTip("현재 시간대의 안내만 닫습니다. 다음 시간 알림은 유지됩니다.")
        self.dismiss_button.setObjectName("primaryButton")
        for button in (self.snooze_button, self.dismiss_button):
            button.setAutoDefault(False)
        self.snooze_button.clicked.connect(lambda: self.snoozeRequested.emit(self._key))
        self.dismiss_button.clicked.connect(lambda: self.dismissRequested.emit(self._key))
        row.addWidget(self.snooze_button)
        row.addWidget(self.dismiss_button)
        more = QToolButton()
        more.setText("...")
        more.setStyleSheet(
            "QToolButton { color: #172033; background: #F4F6FA; border: 1px solid #D5DBE7; "
            "border-radius: 8px; padding: 6px; } QToolButton:hover { background: #DFE9FF; }"
        )
        more.setToolTip("알림 설정 / 이번 실행 동안 끄기")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        menu.addAction("이번 실행 동안 끄기", self.disableRequested.emit)
        menu.addAction("알림 설정...", self.settingsRequested.emit)
        more.setMenu(menu)
        row.addWidget(more)
        root.addLayout(row)

    def update_state(self, state: HourlyState) -> None:
        self._key = state.bucket.key
        self.message_label.setText(state.message)
        self.hour_label.setText(
            f"{state.bucket.starts_at:%H}:00 ~ {state.bucket.starts_at:%H}:59"
        )
        self.next_label.setText(
            f"다음 알림 {format_next(state.next_regular_at, state.bucket.starts_at)}"
        )
        self.early_hint.setVisible(state.early_morning_hint)
        self.snooze_button.setEnabled(state.can_snooze)
        self.snooze_button.setToolTip(
            "" if state.can_snooze else "이번 시간 안에 5분 뒤 재알림 불가"
        )
        signature = (state.message, state.early_morning_hint)
        if signature != self._content_signature:
            self._content_signature = signature
            width = self.width()
            text_height = self.message_label.fontMetrics().boundingRect(
                QRect(0, 0, width - 32, 10_000), Qt.TextFlag.TextWordWrap, state.message
            ).height()
            self.message_label.setMinimumHeight(text_height + 4)
            layout = self.layout()
            if layout is not None:
                layout.activate()
                self.resize(width, max(220, layout.minimumSize().height(), layout.totalHeightForWidth(width)))

    def reject(self) -> None:
        if not self._closing:
            self.dismissRequested.emit(self._key)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._closing:
            self.dismissRequested.emit(self._key)
        event.accept()

    def shutdown(self) -> None:
        self._closing = True
        self.close()
