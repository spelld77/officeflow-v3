"""Whole-value time entry and intentional (non-wheel) schedule changes."""

import re
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta

from PySide6.QtCore import QEvent, QObject, QPointF, Qt, QTime, Signal
from PySide6.QtGui import QFocusEvent, QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QWidget,
)


def parse_time_text(text: str) -> time:
    value = text.strip()
    if re.fullmatch(r"[0-9]{1,2}", value):
        hour, minute = int(value), 0
    elif re.fullmatch(r"[0-9]{3,4}", value):
        hour, minute = int(value[:-2]), int(value[-2:])
    elif re.fullmatch(r"[0-9]{1,2}:[0-9]{1,2}", value):
        hour, minute = map(int, value.split(":"))
    else:
        raise ValueError("시각을 11, 1124 또는 11:24 형식으로 입력하세요.")
    if hour > 23 or minute > 59:
        raise ValueError("시각은 00:00부터 23:59까지 입력하세요.")
    return time(hour, minute)


def default_start_at(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("현재 시각에 시간대가 필요합니다.")
    threshold = now.astimezone(UTC) + timedelta(hours=1)
    local = threshold.astimezone(now.tzinfo)
    candidate = local.replace(minute=0, second=0, microsecond=0, fold=0)
    # UI stores only date/hour/minute; don't suggest an ambiguous second fold.
    while candidate.astimezone(UTC) < threshold:
        candidate = (candidate + timedelta(hours=1)).astimezone(UTC).astimezone(now.tzinfo)
        candidate = candidate.replace(fold=0)
    return candidate


def mark_input_error(widget: QWidget, invalid: bool) -> None:
    if bool(widget.property("inputError")) != invalid:
        widget.setProperty("inputError", invalid)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()


class WholeTimeEdit(QLineEdit):
    timeChanged = Signal(QTime)
    hourRequested = Signal(int)
    invalidEntry = Signal(str)

    def __init__(self, value: QTime, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value = QTime(value.hour(), value.minute())
        self._select_on_mouse_press = False
        self.setText(self._value.toString("HH:mm"))
        self.setPlaceholderText("HH:mm")
        self.setToolTip("11 → 11:00 · 1124 → 11:24 · ↑/↓ 한 시간 조정")
        self.editingFinished.connect(self._finish_entry)
        self.textEdited.connect(lambda _text: mark_input_error(self, False))

    def time(self) -> QTime:
        value = parse_time_text(self.text())
        return QTime(value.hour, value.minute)

    def setTime(self, value: QTime) -> None:
        if not value.isValid():
            raise ValueError("올바른 시각을 입력하세요.")
        changed = self._value != value
        self._value = QTime(value.hour(), value.minute())
        self.setText(self._value.toString("HH:mm"))
        mark_input_error(self, False)
        if changed:
            self.timeChanged.emit(self._value)

    def commit(self) -> None:
        try:
            value = self.time()
        except ValueError:
            mark_input_error(self, True)
            raise
        self.setTime(value)

    def _finish_entry(self) -> None:
        try:
            self.commit()
        except ValueError as error:
            self.invalidEntry.emit(str(error))

    def focusInEvent(self, event: QFocusEvent) -> None:
        super().focusInEvent(event)
        # Qt may focus the field before dispatching its mousePressEvent.
        self._select_on_mouse_press = event.reason() == Qt.FocusReason.MouseFocusReason
        self.selectAll()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # QLineEdit normally moves the cursor after focusInEvent; select after it.
        entering = not self.hasFocus() or self._select_on_mouse_press
        self._select_on_mouse_press = False
        super().mousePressEvent(event)
        if entering:
            self.selectAll()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.hourRequested.emit(1 if event.key() == Qt.Key.Key_Up else -1)
            event.accept()
            return
        super().keyPressEvent(event)


class TimeInput(QWidget):
    dayStepped = Signal(int)

    def __init__(self, value: QTime, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.allow_day_step = True
        self.can_step_day: Callable[[int], bool] = lambda _days: True
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.edit = WholeTimeEdit(value, self)
        layout.addWidget(self.edit, 1)
        self.minus_button = QPushButton("-", self)
        self.plus_button = QPushButton("+", self)
        for button, delta in ((self.minus_button, -1), (self.plus_button, 1)):
            button.setFixedWidth(32)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setAutoDefault(False)
            label = "한 시간 이전" if delta < 0 else "한 시간 이후"
            button.setAccessibleName(label)
            button.setToolTip(label)
            button.clicked.connect(lambda _checked=False, step=delta: self.step_hour(step))
            layout.addWidget(button)
        self.edit.hourRequested.connect(self.step_hour)

    def step_hour(self, delta: int) -> None:
        try:
            self.edit.commit()
        except ValueError as error:
            self.edit.invalidEntry.emit(str(error))
            self.edit.setFocus()
            return
        current = self.edit.time()
        days, minutes = divmod(current.hour() * 60 + current.minute() + delta * 60, 1440)
        if days and not self.allow_day_step:
            self.edit.invalidEntry.emit("다음 날 종료는 기간 일정을 선택하세요.")
            return
        if days and not self.can_step_day(days):
            self.edit.invalidEntry.emit("날짜 입력 범위를 벗어나는 시각입니다.")
            return
        # Date first: consumers must never see a wrapped time on the old day.
        if days:
            self.dayStepped.emit(days)
        self.edit.setTime(QTime(minutes // 60, minutes % 60))
        self.edit.selectAll()


class WheelScrollGuard(QObject):
    """Protect closed value controls while forwarding wheel motion to the body."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.Wheel or not isinstance(event, QWheelEvent):
            return False
        if isinstance(watched, QComboBox) and watched.view().isVisible():
            return False
        if not isinstance(watched, QWidget):
            return False
        parent = watched.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                viewport = parent.viewport()
                forwarded = QWheelEvent(
                    QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint())),
                    event.globalPosition(),
                    event.pixelDelta(),
                    event.angleDelta(),
                    event.buttons(),
                    event.modifiers(),
                    event.phase(),
                    event.inverted(),
                )
                QApplication.sendEvent(viewport, forwarded)
                break
            parent = parent.parentWidget()
        event.accept()
        return True
