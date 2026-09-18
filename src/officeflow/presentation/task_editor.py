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
from officeflow.domain.enums import ReminderRelation, TaskPriority, TaskStatus
from officeflow.domain.recurrence import (
    RecurrenceFrequency,
    RecurrenceSpec,
    parse_simple_recurrence,
)
from officeflow.domain.reminder import ReminderRuleInput
from officeflow.domain.task import Task, TaskValidationError


class TaskEditorDialog(QDialog):
    def __init__(
        self,
        *,
        timezone: str,
        task: Task | None = None,
        reminder_rules: tuple[ReminderRuleInput, ...] = (),
        initial_date: date | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._timezone = timezone
        self._task = task
        self._draft: TaskDraft | None = None
        self._custom_recurrence_rule: str | None = None
        self._custom_start_reminder: ReminderRuleInput | None = None
        self._custom_end_reminder: ReminderRuleInput | None = None
        self._preserved_reminders: tuple[ReminderRuleInput, ...] = ()

        self.setWindowTitle("업무 수정" if task else "새 업무")
        self.setModal(True)
        self.resize(520, 600)
        self.setMinimumSize(460, 520)

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

        self.start_reminder_combo = QComboBox()
        self.start_reminder_combo.setObjectName("taskStartReminderCombo")
        self._add_reminder_options(self.start_reminder_combo, include_all_day_suggestion=True)
        self.schedule_form.addRow("시작 알림", self.start_reminder_combo)

        self.advanced_schedule_check = QCheckBox("종료·반복 등 추가 설정 보기")
        self.advanced_schedule_check.setObjectName("advancedScheduleCheck")
        self.schedule_form.addRow("", self.advanced_schedule_check)

        self.end_enabled_check = QCheckBox("종료 시각 지정")
        self.end_enabled_check.setObjectName("taskEndEnabledCheck")
        self.schedule_form.addRow("", self.end_enabled_check)

        self.end_date_edit = QDateEdit(QDate(default_end.year, default_end.month, default_end.day))
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.schedule_form.addRow("종료일", self.end_date_edit)

        self.end_time_edit = QTimeEdit(QTime(default_end.hour, default_end.minute))
        self.end_time_edit.setDisplayFormat("HH:mm")
        self.schedule_form.addRow("종료 시각", self.end_time_edit)

        self.end_reminder_combo = QComboBox()
        self.end_reminder_combo.setObjectName("taskEndReminderCombo")
        self._add_reminder_options(self.end_reminder_combo)
        self.schedule_form.addRow("종료 알림", self.end_reminder_combo)

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
        self.advanced_schedule_check.toggled.connect(self._update_schedule_visibility)
        self.end_enabled_check.toggled.connect(self._update_schedule_visibility)
        self.start_date_edit.dateChanged.connect(self._keep_end_date_valid)
        self.repeat_combo.currentIndexChanged.connect(self._update_recurrence_visibility)
        self.repeat_until_check.toggled.connect(self._update_recurrence_visibility)

        if task is not None:
            self._populate(task)
            self._populate_reminders(reminder_rules)
        else:
            self.schedule_combo.setCurrentIndex(1)
            self.all_day_check.setChecked(False)
            self.start_reminder_combo.setCurrentIndex(
                self.start_reminder_combo.findData(0)
            )
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
            if all_day:
                end_date = (
                    cast(date, self.end_date_edit.date().toPython())
                    if schedule_type == "range"
                    else start_date
                )
                if end_date < start_date:
                    raise TaskValidationError("종료일은 시작일보다 빠를 수 없습니다.")
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
                has_end = schedule_type == "range" or self.end_enabled_check.isChecked()
                if has_end:
                    end_date = (
                        cast(date, self.end_date_edit.date().toPython())
                        if schedule_type == "range"
                        else start_date
                    )
                    if end_date < start_date:
                        raise TaskValidationError("종료일은 시작일보다 빠를 수 없습니다.")
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
        reminder_rules = (
            self._build_reminder_rules(include_end=ends_at is not None)
            if schedule_type != "none"
            else ()
        )
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
            reminder_rules=reminder_rules,
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
        if not task.all_day and task.ends_at is not None:
            self.end_enabled_check.setChecked(True)
            self.advanced_schedule_check.setChecked(True)
        if schedule_type == "range":
            self.advanced_schedule_check.setChecked(True)
        if task.recurrence_rule:
            self.advanced_schedule_check.setChecked(True)
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
        if is_range and not self.advanced_schedule_check.isChecked():
            self.advanced_schedule_check.setChecked(True)
        advanced = has_schedule and self.advanced_schedule_check.isChecked()
        has_end = (
            (has_schedule and self.all_day_check.isChecked())
            or is_range
            or self.end_enabled_check.isChecked()
        )
        self._set_row_visible(self.all_day_check, has_schedule)
        self._set_row_visible(self.start_date_edit, has_schedule)
        self._set_row_visible(self.start_time_edit, is_timed)
        self._set_row_visible(self.start_reminder_combo, has_schedule)
        self._set_row_visible(self.advanced_schedule_check, has_schedule)
        self.advanced_schedule_check.setText(
            "종료·반복 등 추가 설정 접기"
            if advanced
            else "종료·반복 등 추가 설정 보기"
        )
        target_height = 720 if advanced else 600
        if self.height() != target_height:
            self.resize(self.width(), target_height)
        self._set_row_visible(self.end_enabled_check, advanced and is_timed and not is_range)
        self._set_row_visible(self.end_date_edit, advanced and is_range)
        self._set_row_visible(self.end_time_edit, advanced and is_timed and has_end)
        self._set_row_visible(self.end_reminder_combo, advanced and has_end)
        self._set_row_visible(self.repeat_combo, advanced)
        self._update_recurrence_visibility()

    def _update_recurrence_visibility(self) -> None:
        has_schedule = str(self.schedule_combo.currentData()) != "none"
        advanced = has_schedule and self.advanced_schedule_check.isChecked()
        repeat_value = str(self.repeat_combo.currentData())
        has_repeat = advanced and bool(repeat_value)
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
            self.start_reminder_combo,
            self.advanced_schedule_check,
            self.end_enabled_check,
            self.end_date_edit,
            self.end_time_edit,
            self.end_reminder_combo,
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

    @staticmethod
    def _add_reminder_options(
        combo: QComboBox,
        *,
        include_all_day_suggestion: bool = False,
    ) -> None:
        combo.addItem("알림 없음", None)
        combo.addItem("기준 시각", 0)
        combo.addItem("5분 전", -5)
        combo.addItem("10분 전", -10)
        combo.addItem("30분 전", -30)
        combo.addItem("1시간 전", -60)
        combo.addItem("1일 전", -1_440)
        if include_all_day_suggestion:
            combo.addItem("당일 오전 9시 (종일 일정)", 540)

    def _populate_reminders(self, rules: tuple[ReminderRuleInput, ...]) -> None:
        preserved: list[ReminderRuleInput] = []
        populated: set[ReminderRelation] = set()
        for rule in rules:
            if rule.relation not in {ReminderRelation.START, ReminderRelation.END}:
                preserved.append(rule)
                continue
            if rule.relation in populated:
                preserved.append(rule)
                continue
            combo = (
                self.start_reminder_combo
                if rule.relation is ReminderRelation.START
                else self.end_reminder_combo
            )
            if rule.relation is ReminderRelation.END:
                self.advanced_schedule_check.setChecked(True)
            index = combo.findData(rule.offset_minutes)
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.addItem("사용자 정의 알림 유지", "custom")
                combo.setCurrentIndex(combo.count() - 1)
                if rule.relation is ReminderRelation.START:
                    self._custom_start_reminder = rule
                else:
                    self._custom_end_reminder = rule
            populated.add(rule.relation)
        self._preserved_reminders = tuple(preserved)

    def _build_reminder_rules(
        self,
        *,
        include_end: bool,
    ) -> tuple[ReminderRuleInput, ...]:
        rules: list[ReminderRuleInput] = list(self._preserved_reminders)
        for relation, combo, custom in (
            (
                ReminderRelation.START,
                self.start_reminder_combo,
                self._custom_start_reminder,
            ),
            (
                ReminderRelation.END,
                self.end_reminder_combo,
                self._custom_end_reminder,
            ),
        ):
            if relation is ReminderRelation.END and not include_end:
                continue
            value = combo.currentData()
            if value == "custom" and custom is not None:
                rules.append(custom)
            elif isinstance(value, int):
                rules.append(ReminderRuleInput(relation=relation, offset_minutes=value))
        return tuple(rules)
