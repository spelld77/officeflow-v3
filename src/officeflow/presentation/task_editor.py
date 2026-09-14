from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from typing import cast
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QEvent, QObject, Qt, QTime
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QSpinBox,
    QTextEdit,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.tasks import TaskDraft
from officeflow.domain.enums import TaskPriority, TaskStatus
from officeflow.domain.recurrence import (
    RecurrenceFrequency,
    RecurrenceSpec,
    parse_simple_recurrence,
)
from officeflow.domain.task import Task, TaskValidationError


class TaskEditorDialog(QDialog):
    def __init__(
        self,
        *,
        timezone: str,
        task: Task | None = None,
        initial_date: date | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._timezone = timezone
        self._task = task
        self._draft: TaskDraft | None = None
        self._custom_recurrence_rule: str | None = None

        self.setWindowTitle("업무 수정" if task else "새 업무")
        self.setModal(True)
        self.resize(520, 680)
        self.setMinimumSize(460, 600)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setVerticalSpacing(10)

        self.title_edit = QLineEdit()
        self.title_edit.setObjectName("taskTitleEdit")
        self.title_edit.setMaxLength(200)
        self.title_edit.setPlaceholderText("해야 할 업무를 입력하세요")
        form.addRow("제목 *", self.title_edit)

        self.description_edit = QTextEdit()
        self.description_edit.setObjectName("taskDescriptionEdit")
        self.description_edit.setPlaceholderText("필요한 설명이나 참고 내용을 입력하세요")
        self.description_edit.setMaximumHeight(120)
        self.description_edit.setTabChangesFocus(True)
        form.addRow("설명", self.description_edit)

        self.priority_combo = QComboBox()
        for label, priority_value in (
            ("보통", TaskPriority.NORMAL),
            ("관심", TaskPriority.ATTENTION),
            ("중요", TaskPriority.IMPORTANT),
            ("긴급", TaskPriority.URGENT),
        ):
            self.priority_combo.addItem(label, priority_value.value)
        form.addRow("중요도", self.priority_combo)

        self.status_combo = QComboBox()
        for label, status_value in (
            ("진행", TaskStatus.ACTIVE),
            ("대기", TaskStatus.PENDING),
            ("완료", TaskStatus.COMPLETED),
            ("취소", TaskStatus.CANCELED),
        ):
            self.status_combo.addItem(label, status_value.value)
        form.addRow("상태", self.status_combo)

        self.pinned_check = QCheckBox("중요 업무로 상단에 고정")
        form.addRow("", self.pinned_check)
        root.addLayout(form)

        schedule_group = QGroupBox("일정")
        self.schedule_form = QFormLayout(schedule_group)
        self.schedule_combo = QComboBox()
        self.schedule_combo.addItem("일정 없음", "none")
        self.schedule_combo.addItem("하루 일정", "day")
        self.schedule_combo.addItem("기간 일정", "range")
        self.schedule_form.addRow("유형", self.schedule_combo)

        self.all_day_check = QCheckBox("종일")
        self.schedule_form.addRow("", self.all_day_check)

        zone = ZoneInfo(timezone)
        default_start = (datetime.now(zone) + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )
        default_end = default_start + timedelta(hours=1)
        self.start_date_edit = QDateEdit(
            QDate(default_start.year, default_start.month, default_start.day)
        )
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.schedule_form.addRow("시작일", self.start_date_edit)

        self.start_time_edit = QTimeEdit(QTime(default_start.hour, default_start.minute))
        self.start_time_edit.setDisplayFormat("HH:mm")
        self.schedule_form.addRow("시작 시각", self.start_time_edit)

        self.end_date_edit = QDateEdit(QDate(default_end.year, default_end.month, default_end.day))
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.schedule_form.addRow("종료일", self.end_date_edit)

        self.end_time_edit = QTimeEdit(QTime(default_end.hour, default_end.minute))
        self.end_time_edit.setDisplayFormat("HH:mm")
        self.schedule_form.addRow("종료 시각", self.end_time_edit)

        self.repeat_combo = QComboBox()
        self.repeat_combo.setObjectName("taskRepeatCombo")
        self.repeat_combo.addItem("반복 안 함", "")
        self.repeat_combo.addItem("매일", RecurrenceFrequency.DAILY.value)
        self.repeat_combo.addItem("매주", RecurrenceFrequency.WEEKLY.value)
        self.repeat_combo.addItem("매월", RecurrenceFrequency.MONTHLY.value)
        self.schedule_form.addRow("반복", self.repeat_combo)

        self.repeat_interval = QSpinBox()
        self.repeat_interval.setObjectName("taskRepeatInterval")
        self.repeat_interval.setRange(1, 99)
        self.repeat_interval.setValue(1)
        self.repeat_interval.setSuffix("회 간격")
        self.schedule_form.addRow("간격", self.repeat_interval)

        self.repeat_until_check = QCheckBox("반복 종료일 지정")
        self.repeat_until_check.setObjectName("taskRepeatUntilCheck")
        self.schedule_form.addRow("", self.repeat_until_check)

        default_until = default_start.date() + timedelta(days=30)
        self.repeat_until_date = QDateEdit(
            QDate(default_until.year, default_until.month, default_until.day)
        )
        self.repeat_until_date.setObjectName("taskRepeatUntilDate")
        self.repeat_until_date.setCalendarPopup(True)
        self.repeat_until_date.setDisplayFormat("yyyy-MM-dd")
        self.schedule_form.addRow("반복 종료", self.repeat_until_date)
        self._schedule_value_edits = (
            self.start_date_edit,
            self.start_time_edit,
            self.end_date_edit,
            self.end_time_edit,
            self.repeat_until_date,
        )
        for edit in self._schedule_value_edits:
            edit.installEventFilter(self)
        root.addWidget(schedule_group)

        self.error_label = QLabel()
        self.error_label.setObjectName("formError")
        self.error_label.setStyleSheet("color: #C62828; font-weight: 600;")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        root.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("저장")
        self.save_button.setObjectName("primaryButton")
        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setText("취소")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.schedule_combo.currentIndexChanged.connect(self._update_schedule_visibility)
        self.all_day_check.toggled.connect(self._update_schedule_visibility)
        self.start_date_edit.dateChanged.connect(self._keep_end_date_valid)
        self.repeat_combo.currentIndexChanged.connect(self._update_recurrence_visibility)
        self.repeat_until_check.toggled.connect(self._update_recurrence_visibility)

        if task is not None:
            self._populate(task)
        else:
            self.schedule_combo.setCurrentIndex(1)
            self.all_day_check.setChecked(True)
            if initial_date is not None:
                selected = QDate(initial_date.year, initial_date.month, initial_date.day)
                self.start_date_edit.setDate(selected)
                self.end_date_edit.setDate(selected)
                repeat_until = initial_date + timedelta(days=30)
                self.repeat_until_date.setDate(
                    QDate(repeat_until.year, repeat_until.month, repeat_until.day)
                )
        self._update_schedule_visibility()
        self._update_recurrence_visibility()
        self._configure_tab_order()
        self.title_edit.setFocus()

    def draft(self) -> TaskDraft:
        if self._draft is None:
            raise RuntimeError("대화상자가 아직 저장되지 않았습니다.")
        return self._draft

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched in self._schedule_value_edits and event.type() is QEvent.Type.KeyPress:
            key_event = cast(QKeyEvent, event)
            if key_event.key() == Qt.Key.Key_Tab:
                return self.focusNextPrevChild(True)
            if key_event.key() == Qt.Key.Key_Backtab:
                return self.focusNextPrevChild(False)
        return super().eventFilter(watched, event)

    def _validate_and_accept(self) -> None:
        try:
            self._draft = self._build_draft()
            Task.create(
                title=self._draft.title,
                description=self._draft.description,
                priority=self._draft.priority,
                status=self._draft.status,
                is_pinned=self._draft.is_pinned,
                all_day=self._draft.all_day,
                starts_at=self._draft.starts_at,
                ends_at=self._draft.ends_at,
                timezone=self._draft.timezone,
                recurrence_rule=self._draft.recurrence_rule,
            )
        except (TaskValidationError, ValueError) as error:
            self.error_label.setText(str(error))
            self.error_label.show()
            return
        self.accept()

    def _build_draft(self) -> TaskDraft:
        schedule_type = str(self.schedule_combo.currentData())
        starts_at: datetime | None = None
        ends_at: datetime | None = None
        all_day = schedule_type != "none" and self.all_day_check.isChecked()
        if schedule_type != "none":
            zone = ZoneInfo(self._timezone)
            start_date = cast(date, self.start_date_edit.date().toPython())
            end_date = (
                cast(date, self.end_date_edit.date().toPython())
                if schedule_type == "range"
                else start_date
            )
            if end_date < start_date:
                raise TaskValidationError("종료일은 시작일보다 빠를 수 없습니다.")
            if all_day:
                starts_at = datetime.combine(start_date, time.min, tzinfo=zone).astimezone(UTC)
                ends_at = datetime.combine(
                    end_date + timedelta(days=1), time.min, tzinfo=zone
                ).astimezone(UTC)
            else:
                starts_at = datetime.combine(
                    start_date,
                    cast(time, self.start_time_edit.time().toPython()),
                    tzinfo=zone,
                ).astimezone(UTC)
                ends_at = datetime.combine(
                    end_date,
                    cast(time, self.end_time_edit.time().toPython()),
                    tzinfo=zone,
                ).astimezone(UTC)
        recurrence_rule: str | None = None
        repeat_value = str(self.repeat_combo.currentData())
        if schedule_type != "none" and repeat_value == "custom":
            recurrence_rule = self._custom_recurrence_rule
        elif schedule_type != "none" and repeat_value:
            until = (
                cast(date, self.repeat_until_date.date().toPython())
                if self.repeat_until_check.isChecked()
                else None
            )
            start_day = cast(date, self.start_date_edit.date().toPython())
            if until is not None and until < start_day:
                raise TaskValidationError("반복 종료일은 시작일보다 빠를 수 없습니다.")
            recurrence_rule = RecurrenceSpec(
                frequency=RecurrenceFrequency(repeat_value),
                interval=self.repeat_interval.value(),
                until=until,
            ).to_rrule(self._timezone)
        return TaskDraft(
            title=self.title_edit.text(),
            description=self.description_edit.toPlainText(),
            priority=TaskPriority(str(self.priority_combo.currentData())),
            status=TaskStatus(str(self.status_combo.currentData())),
            is_pinned=self.pinned_check.isChecked(),
            all_day=all_day,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone=self._timezone,
            recurrence_rule=recurrence_rule,
        )

    def _populate(self, task: Task) -> None:
        self.title_edit.setText(task.title)
        self.description_edit.setPlainText(task.description)
        self._set_combo_data(self.priority_combo, task.priority.value)
        self._set_combo_data(self.status_combo, task.status.value)
        self.pinned_check.setChecked(task.is_pinned)

        if task.starts_at is None:
            self._set_combo_data(self.schedule_combo, "none")
            return
        zone = ZoneInfo(task.timezone)
        start = task.starts_at.astimezone(zone)
        end = task.ends_at.astimezone(zone) if task.ends_at else start
        inclusive_end = end - timedelta(microseconds=1) if task.all_day and task.ends_at else end
        schedule_type = "day" if start.date() == inclusive_end.date() else "range"
        self._set_combo_data(self.schedule_combo, schedule_type)
        self.all_day_check.setChecked(task.all_day)
        self.start_date_edit.setDate(QDate(start.year, start.month, start.day))
        self.end_date_edit.setDate(
            QDate(inclusive_end.year, inclusive_end.month, inclusive_end.day)
        )
        self.start_time_edit.setTime(QTime(start.hour, start.minute))
        self.end_time_edit.setTime(QTime(end.hour, end.minute))
        if task.recurrence_rule:
            recurrence = parse_simple_recurrence(task.recurrence_rule, task.timezone)
            if recurrence is None:
                self._custom_recurrence_rule = task.recurrence_rule
                self.repeat_combo.addItem("사용자 정의 반복 유지", "custom")
                self._set_combo_data(self.repeat_combo, "custom")
            else:
                self._set_combo_data(self.repeat_combo, recurrence.frequency.value)
                self.repeat_interval.setValue(recurrence.interval)
                if recurrence.until is not None:
                    self.repeat_until_check.setChecked(True)
                    self.repeat_until_date.setDate(
                        QDate(
                            recurrence.until.year,
                            recurrence.until.month,
                            recurrence.until.day,
                        )
                    )

    def _update_schedule_visibility(self) -> None:
        schedule_type = str(self.schedule_combo.currentData())
        has_schedule = schedule_type != "none"
        is_range = schedule_type == "range"
        is_timed = has_schedule and not self.all_day_check.isChecked()
        self._set_row_visible(self.all_day_check, has_schedule)
        self._set_row_visible(self.start_date_edit, has_schedule)
        self._set_row_visible(self.start_time_edit, is_timed)
        self._set_row_visible(self.end_date_edit, has_schedule and is_range)
        self._set_row_visible(self.end_time_edit, is_timed)
        self._set_row_visible(self.repeat_combo, has_schedule)
        self._update_recurrence_visibility()

    def _update_recurrence_visibility(self) -> None:
        has_schedule = str(self.schedule_combo.currentData()) != "none"
        repeat_value = str(self.repeat_combo.currentData())
        has_repeat = has_schedule and bool(repeat_value)
        is_editable = has_repeat and repeat_value != "custom"
        self._set_row_visible(self.repeat_interval, is_editable)
        self._set_row_visible(self.repeat_until_check, is_editable)
        self._set_row_visible(
            self.repeat_until_date,
            is_editable and self.repeat_until_check.isChecked(),
        )

    def _set_row_visible(self, field: QWidget, visible: bool) -> None:
        label = self.schedule_form.labelForField(field)
        if label is not None:
            label.setVisible(visible)
        field.setVisible(visible)

    def _keep_end_date_valid(self, start_date: QDate) -> None:
        self.end_date_edit.setMinimumDate(start_date)
        if self.end_date_edit.date() < start_date:
            self.end_date_edit.setDate(start_date)
        self.repeat_until_date.setMinimumDate(start_date)
        if self.repeat_until_date.date() < start_date:
            self.repeat_until_date.setDate(start_date.addDays(30))

    def _configure_tab_order(self) -> None:
        fields: tuple[QWidget, ...] = (
            self.title_edit,
            self.description_edit,
            self.priority_combo,
            self.status_combo,
            self.pinned_check,
            self.schedule_combo,
            self.all_day_check,
            self.start_date_edit,
            self.start_time_edit,
            self.end_date_edit,
            self.end_time_edit,
            self.repeat_combo,
            self.repeat_interval,
            self.repeat_until_check,
            self.repeat_until_date,
            self.save_button,
            self.cancel_button,
        )
        for current, following in pairwise(fields):
            self.setTabOrder(current, following)

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)
