"""Temporary, modeless reminder edits; the owning task performs the final save."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QEvent, QObject, QSignalBlocker, Qt, QTime
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.reminders import ReminderService, ReminderTarget
from officeflow.application.task_saving import ReminderEdit
from officeflow.domain.enums import ReminderDeliveryStatus, ReminderRelation, TaskStatus
from officeflow.domain.reminder import Reminder, ReminderDelivery
from officeflow.domain.reminder import ReminderRuleInput as Rule
from officeflow.domain.task import Task
from officeflow.presentation.dates import date_with_weekday, format_datetime


def candidate(edit: ReminderEdit, task: Task, now: datetime) -> datetime | None:
    r = edit.rule
    if (
        not r.enabled
        or task.status not in {TaskStatus.ACTIVE, TaskStatus.PENDING}
        or task.deleted_at
    ):
        return None
    reminder = Reminder(
        edit.reminder_id, task.id or 0, r.relation, r.offset_minutes, r.absolute_at, r.enabled
    )
    result = ReminderService._next_candidate(
        ReminderTarget(reminder, task), after=now.replace(second=0, microsecond=0), inclusive=True
    )
    return result[1] if result else None


def rule_label(edit: ReminderEdit, task: Task, now: datetime) -> str:
    r = edit.rule
    due = candidate(edit, task, now)
    if r.relation is ReminderRelation.ABSOLUTE:
        reason = "날짜 지정 · 1회"
        due = r.absolute_at
    else:
        base = "시작" if r.relation is ReminderRelation.START else "종료"
        minutes = r.offset_minutes or 0
        days, remainder = divmod(abs(minutes), 1440)
        hours, remainder = divmod(remainder, 60)
        interval = " ".join(
            text
            for amount, text in (
                (days, f"{days}일"),
                (hours, f"{hours}시간"),
                (remainder, f"{remainder}분"),
            )
            if amount
        )
        reason = (
            f"{base} 시각" if not minutes else f"{base} {interval} {'전' if minutes < 0 else '후'}"
        )
        if task.recurrence_rule:
            reason += " · 반복"
        elif due is None:
            base_at = task.starts_at if r.relation is ReminderRelation.START else task.ends_at
            if base_at is not None:
                due = ReminderService._relative_due(task, base_at, minutes)
    state = (
        "사용 안 함"
        if not r.enabled
        else "예정"
        if due and due >= now.replace(second=0, microsecond=0)
        else "지난 예약"
    )
    if r.enabled and task.status not in {TaskStatus.ACTIVE, TaskStatus.PENDING}:
        state = "업무 완료·취소 · 울리지 않음"
    return f"{format_datetime(due, task.timezone, short=True) if due else '다음 예약 없음'} · {reason} · {state}"


def state_label(
    edit: ReminderEdit, task: Task, now: datetime, deliveries: tuple[ReminderDelivery, ...]
) -> str:
    label = rule_label(edit, task, now)
    history = _matching_history(edit, task, deliveries)
    active = tuple(
        d
        for d in history
        if d.status in {ReminderDeliveryStatus.FIRED, ReminderDeliveryStatus.SNOOZED}
    )
    if active:
        snoozes = [d.snoozed_until for d in active if d.snoozed_until]
        pending = sum(d.status is ReminderDeliveryStatus.FIRED for d in active)
        extra = f"미확인 {pending}건" if pending else ""
        if snoozes:
            extra += (
                (" · " if extra else "")
                + "재알림 "
                + format_datetime(min(snoozes), task.timezone, short=True)
            )
        return label.rsplit(" · ", 1)[0] + " · " + extra
    if history and _unhandled_candidate(edit, task, now, history) is None:
        state = {
            ReminderDeliveryStatus.ACKNOWLEDGED: "확인함",
            ReminderDeliveryStatus.COMPLETED: "완료",
            ReminderDeliveryStatus.DEFERRED: "대기 처리",
        }.get(history[0].status)
        if state:
            return label.rsplit(" · ", 1)[0] + " · " + state
    return label


def _matching_history(
    edit: ReminderEdit, task: Task, deliveries: tuple[ReminderDelivery, ...]
) -> tuple[ReminderDelivery, ...]:
    r = edit.rule
    reminder = Reminder(
        edit.reminder_id, task.id or 0, r.relation, r.offset_minutes, r.absolute_at, r.enabled
    )
    target = ReminderTarget(reminder, task)
    return tuple(
        d
        for d in deliveries
        if d.reminder_id == edit.reminder_id
        and (
            expected := ReminderService._next_candidate(
                target, after=d.scheduled_at, inclusive=True
            )
        )
        is not None
        and expected == (d.occurrence_start, d.scheduled_at)
    )


def _unhandled_candidate(
    edit: ReminderEdit, task: Task, now: datetime, history: tuple[ReminderDelivery, ...]
) -> datetime | None:
    due = candidate(edit, task, now)
    handled = {
        d.scheduled_at
        for d in history
        if d.status
        in {
            ReminderDeliveryStatus.ACKNOWLEDGED,
            ReminderDeliveryStatus.COMPLETED,
            ReminderDeliveryStatus.DEFERRED,
        }
    }
    r = edit.rule
    target = ReminderTarget(
        Reminder(
            edit.reminder_id, task.id or 0, r.relation, r.offset_minutes, r.absolute_at, r.enabled
        ),
        task,
    )
    while due is not None and due in handled:
        result = ReminderService._next_candidate(target, after=due, inclusive=False)
        due = result[1] if result else None
    return due


def reminder_summary(
    edits: tuple[ReminderEdit, ...],
    task: Task,
    now: datetime,
    deliveries: tuple[ReminderDelivery, ...] = (),
) -> str:
    active = tuple(
        d
        for e in edits
        for d in _matching_history(e, task, deliveries)
        if e.rule.enabled
        and d.status in {ReminderDeliveryStatus.FIRED, ReminderDeliveryStatus.SNOOZED}
    )
    dates = [
        due
        for e in edits
        if (due := _unhandled_candidate(e, task, now, _matching_history(e, task, deliveries)))
        is not None
    ]
    dates.extend(d.snoozed_until for d in active if d.snoozed_until is not None)
    disabled = sum(not e.rule.enabled for e in edits)
    text = f"{'반복 알림 ' if task.recurrence_rule else ''}설정 {len(edits)}개"
    if disabled:
        text += f" · 사용 안 함 {disabled}개"
    if any(d.status is ReminderDeliveryStatus.FIRED for d in active):
        text += " · 확인할 알림 있음"
    return (
        text
        + "\n"
        + (
            "다음: " + format_datetime(min(dates), task.timezone, short=True)
            if dates
            else "다음 예약 없음"
        )
    )


class ReminderRuleDialog(QDialog):
    def __init__(
        self, task: Task, *, edit: ReminderEdit | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._task = task
        self._original = edit
        self.result_edit: ReminderEdit | None = None
        self.setWindowTitle("알림 수정" if edit else "알림 추가")
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem("날짜·시각 직접 지정 (1회)", "absolute")
        self.mode.addItem("시작 기준", "start")
        self.mode.addItem("종료 기준", "end")
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm")
        self.date_edit.installEventFilter(self)
        self.time_edit.installEventFilter(self)
        default = datetime.now(ZoneInfo(task.timezone)) + timedelta(hours=1)
        existing_time = edit.rule.absolute_at if edit else None
        if edit and edit.rule.relation is not ReminderRelation.ABSOLUTE:
            existing_time = candidate(edit, task, datetime.now(UTC))
            base = task.starts_at if edit.rule.relation is ReminderRelation.START else task.ends_at
            if existing_time is None and base is not None:
                existing_time = ReminderService._relative_due(
                    task, base, edit.rule.offset_minutes or 0
                )
        value = existing_time.astimezone(ZoneInfo(task.timezone)) if existing_time else default
        self.date_edit.setDate(QDate(value.year, value.month, value.day))
        self.time_edit.setTime(QTime(value.hour, value.minute))
        self.offset = QSpinBox()
        self.offset.setRange(0, 43200)
        self.unit = QComboBox()
        for label, unit_minutes in (("분", 1), ("시간", 60), ("일", 1440)):
            self.unit.addItem(label, unit_minutes)
        self.direction = QComboBox()
        self.direction.addItem("전", -1)
        self.direction.addItem("후", 1)
        self.interval_row = QWidget()
        interval_layout = QHBoxLayout(self.interval_row)
        interval_layout.setContentsMargins(0, 0, 0, 0)
        interval_layout.setSpacing(6)
        interval_layout.addWidget(self.offset, 1)
        interval_layout.addWidget(self.unit)
        interval_layout.addWidget(self.direction)
        self.quick = QComboBox()
        for text, minutes in (
            ("간격 직접 입력", None),
            ("기준 시각", 0),
            ("10분 전", -10),
            ("1시간 전", -60),
            ("1일 전", -1440),
            ("10일 전", -14400),
            ("20일 전", -28800),
        ):
            self.quick.addItem(text, minutes)
        self.quick.currentIndexChanged.connect(self._quick_changed)
        self.enabled = QCheckBox("이 알림 사용")
        self.enabled.setChecked(edit.rule.enabled if edit else True)
        self.date_row = date_with_weekday(self.date_edit)
        form.addRow("방식", self.mode)
        form.addRow("날짜", self.date_row)
        form.addRow("시각", self.time_edit)
        form.addRow("빠른 선택", self.quick)
        form.addRow("간격", self.interval_row)
        form.addRow("", self.enabled)
        self._form = form
        layout.addLayout(form)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color:#C62828")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("적용" if edit else "추가")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self._accept_rule)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if edit:
            self.mode.setCurrentIndex(self.mode.findData(edit.rule.relation.value))
            self._load_offset(edit.rule.offset_minutes or 0)
        self.mode.currentIndexChanged.connect(self._refresh)
        self.date_edit.dateChanged.connect(self._refresh)
        self.time_edit.timeChanged.connect(self._refresh)
        self.offset.valueChanged.connect(self._interval_changed)
        self.unit.currentIndexChanged.connect(self._interval_changed)
        self.direction.currentIndexChanged.connect(self._interval_changed)
        self.enabled.toggled.connect(self._refresh)
        self._refresh()

    def _quick_changed(self) -> None:
        value = self.quick.currentData()
        if isinstance(value, int):
            self._load_offset(value)
            self._refresh()

    def _load_offset(self, minutes: int) -> None:
        multiplier = (
            1440 if minutes and minutes % 1440 == 0 else 60 if minutes and minutes % 60 == 0 else 1
        )
        with QSignalBlocker(self.offset), QSignalBlocker(self.unit), QSignalBlocker(self.direction):
            self.unit.setCurrentIndex(self.unit.findData(multiplier))
            self.offset.setMaximum(43200 // multiplier)
            self.offset.setValue(abs(minutes) // multiplier)
            self.direction.setCurrentIndex(self.direction.findData(-1 if minutes <= 0 else 1))

    def _offset_minutes(self) -> int:
        return (
            self.offset.value() * int(self.unit.currentData()) * int(self.direction.currentData())
        )

    def _interval_changed(self) -> None:
        self.offset.setMaximum(43200 // int(self.unit.currentData()))
        if (
            self.quick.currentData() is not None
            and self.quick.currentData() != self._offset_minutes()
        ):
            with QSignalBlocker(self.quick):
                self.quick.setCurrentIndex(0)
        self._refresh()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched in (self.date_edit, self.time_edit) and event.type() is QEvent.Type.KeyPress:
            key = cast(QKeyEvent, event).key()
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                return self.focusNextPrevChild(key == Qt.Key.Key_Tab)
        return super().eventFilter(watched, event)

    def _edit(self) -> ReminderEdit:
        relation = ReminderRelation(self.mode.currentData())
        if relation is ReminderRelation.ABSOLUTE:
            value = datetime.combine(
                cast(date, self.date_edit.date().toPython()),
                cast(time, self.time_edit.time().toPython()),
                tzinfo=ZoneInfo(self._task.timezone),
            ).astimezone(UTC)
            rule = Rule(relation, absolute_at=value, enabled=self.enabled.isChecked())
        else:
            base = (
                self._task.starts_at if relation is ReminderRelation.START else self._task.ends_at
            )
            if base is None:
                raise ValueError(
                    "이 기준 시각이 없습니다. 업무 일정을 지정하거나 날짜 직접 지정을 사용하세요."
                )
            rule = Rule(
                relation, offset_minutes=self._offset_minutes(), enabled=self.enabled.isChecked()
            )
        return ReminderEdit(rule, self._original.reminder_id if self._original else None)

    def _refresh(self) -> None:
        absolute = self.mode.currentData() == "absolute"
        for field, visible in (
            (self.date_row, absolute),
            (self.time_edit, absolute),
            (self.quick, not absolute),
            (self.interval_row, not absolute),
        ):
            self._form.setRowVisible(field, visible)
        try:
            self.preview.setText(rule_label(self._edit(), self._task, datetime.now(UTC)))
        except ValueError as error:
            self.preview.setText(str(error))

    def _accept_rule(self) -> None:
        try:
            edit = self._edit()
            unchanged = self._original is not None and self._original.rule == edit.rule
            if (
                edit.rule.enabled
                and self._task.status in {TaskStatus.ACTIVE, TaskStatus.PENDING}
                and not unchanged
                and candidate(edit, self._task, datetime.now(UTC)) is None
            ):
                raise ValueError("새 알림은 현재 분 이후로 지정하세요.")
            self.result_edit = edit
        except ValueError as error:
            self.error.setText(str(error))
            return
        self.accept()


class ReminderSettingsDialog(QDialog):
    def __init__(
        self,
        task: Task,
        edits: tuple[ReminderEdit, ...],
        *,
        save: Callable[[tuple[ReminderEdit, ...]], None] | None = None,
        deliveries: tuple[ReminderDelivery, ...] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._task = task
        self._edits = list(edits)
        self._save = save
        self._original_edits = edits
        self._deliveries = deliveries
        self._child: ReminderRuleDialog | None = None
        self.setWindowTitle(f"알림 설정 · {task.title}")
        self.resize(570, 400)
        self.setMinimumSize(400, 300)
        layout = QVBoxLayout(self)
        self.count_label = QLabel()
        layout.addWidget(self.count_label)
        self.list = QListWidget()
        self.list.setObjectName("reminderSettingsList")
        self.list.itemDoubleClicked.connect(lambda _item: self._open_rule(True))
        layout.addWidget(self.list, 1)
        actions = QHBoxLayout()
        self.add_button = QPushButton("추가…")
        self.edit_button = QPushButton("수정…")
        self.delete_button = QPushButton("삭제")
        self.list.currentRowChanged.connect(self._buttons)
        for button in (self.add_button, self.edit_button, self.delete_button):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.add_button.clicked.connect(lambda: self._open_rule(False))
        self.edit_button.clicked.connect(lambda: self._open_rule(True))
        self.delete_button.clicked.connect(self._delete)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText(
            "저장" if save else "적용"
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.buttons.accepted.connect(self._apply)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._refresh()

    def edits(self) -> tuple[ReminderEdit, ...]:
        return tuple(self._edits)

    def update_delivery_states(self, deliveries: tuple[ReminderDelivery, ...]) -> None:
        if deliveries == self._deliveries:
            return
        self._deliveries = deliveries
        self._refresh()

    def update_basis(self, task: Task) -> None:
        self._task = task
        if self._child:
            self._child._task = task
            self._child._refresh()
        self._refresh()

    def _refresh(self) -> None:
        selected = (
            self.list.currentItem().data(Qt.ItemDataRole.UserRole)
            if self.list.currentItem()
            else None
        )
        scroll = self.list.verticalScrollBar().value()
        if self._child is None:
            self._edits.sort(
                key=lambda e: (
                    e.rule.absolute_at
                    or candidate(e, self._task, datetime.now(UTC))
                    or datetime.max.replace(tzinfo=UTC)
                )
            )
        row = self.list.currentRow()
        self.list.clear()
        for edit in self._edits:
            history = self._deliveries if edit in self._original_edits else ()
            label = state_label(edit, self._task, datetime.now(UTC), history)
            self.list.addItem(label)
            item = self.list.item(self.list.count() - 1)
            item.setToolTip(label)
            item.setData(Qt.ItemDataRole.UserRole, edit)
        self.count_label.setText(
            f"설정 {len(self._edits)} / 최대 20개 · 실제 울릴 시각은 오늘·달력에서도 확인"
        )
        self.count_label.setWordWrap(True)
        selected_row = next(
            (
                index
                for index, edit in enumerate(self._edits)
                if edit == selected
                or (
                    selected is not None
                    and edit.reminder_id is not None
                    and edit.reminder_id == selected.reminder_id
                )
            ),
            None,
        )
        self.list.setCurrentRow(
            selected_row if selected_row is not None else min(max(row, 0), self.list.count() - 1)
        )
        self.list.verticalScrollBar().setValue(scroll)
        self._buttons()

    def _buttons(self) -> None:
        selected = self.list.currentRow() >= 0
        self.add_button.setEnabled(len(self._edits) < 20 and self._child is None)
        self.edit_button.setEnabled(selected and self._child is None)
        self.delete_button.setEnabled(selected and self._child is None)

    def _delete(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            if any(
                d.reminder_id == self._edits[row].reminder_id
                and d.status in {ReminderDeliveryStatus.FIRED, ReminderDeliveryStatus.SNOOZED}
                for d in self._deliveries
            ):
                self.error.setText(
                    "최종 저장하면 이 알림의 미확인·재알림과 발생 이력도 제거됩니다. 취소하면 유지됩니다."
                )
            del self._edits[row]
            self._refresh()

    def _open_rule(self, editing: bool) -> None:
        if self._child is not None:
            self._child.raise_()
            return
        row = self.list.currentRow() if editing else -1
        if (editing and row < 0) or (not editing and len(self._edits) >= 20):
            return
        child = ReminderRuleDialog(
            self._task, edit=self._edits[row] if editing else None, parent=self
        )
        self._child = child
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
        self._buttons()

        def finished(result: int) -> None:
            self._child = None
            self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(True)
            if result == QDialog.DialogCode.Accepted and child.result_edit:
                edit = child.result_edit
                if any(
                    e.rule.identity == edit.rule.identity
                    for i, e in enumerate(self._edits)
                    if i != row
                ):
                    self.error.setText("동일한 알림이 이미 있습니다.")
                else:
                    if editing:
                        self._edits[row] = edit
                    else:
                        self._edits.append(edit)
                    self.error.clear()
            child.deleteLater()
            self._refresh()

        child.finished.connect(finished)
        child.show()

    def _apply(self) -> None:
        try:
            if self._save:
                self._save(self.edits())
        except Exception as error:
            self.error.setText(str(error))
            return
        self.accept()

    def done(self, result: int) -> None:
        if self._child:
            self._child.reject()
        super().done(result)
