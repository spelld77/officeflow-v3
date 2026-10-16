from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from typing import cast
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QEvent, QObject, Qt, QTime, QTimer, Signal
from PySide6.QtGui import QKeyEvent, QShowEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.attachments import AttachmentService
from officeflow.application.input_review import DateInput, InputNotice, review_dates
from officeflow.application.reminder_runtime import ReminderRuntimeState
from officeflow.application.task_mutations import MutationAttempt
from officeflow.application.task_saving import ReminderEdit
from officeflow.application.tasks import TaskDraft
from officeflow.domain.enums import ReminderRelation, TaskPriority, TaskStatus
from officeflow.domain.recurrence import (
    RecurrenceFrequency,
    RecurrenceSpec,
    parse_simple_recurrence,
)
from officeflow.domain.reminder import Reminder, ReminderDelivery, ReminderRuleInput
from officeflow.domain.task import Task, TaskValidationError
from officeflow.presentation.compact_labels import CompactLabel, WrappingLabel
from officeflow.presentation.dates import date_with_weekday
from officeflow.presentation.image_attachments import (
    ClipboardImagePanel,
    ImageImportController,
    ImageImportResult,
)
from officeflow.presentation.input_controls import (
    TimeInput,
    WheelScrollGuard,
    WholeTimeEdit,
    default_start_at,
    mark_input_error,
)
from officeflow.presentation.input_review import DateCaution, DateReview
from officeflow.presentation.reminder_settings import (
    ReminderSettingsDialog,
    candidate,
    reminder_summary,
)


class TaskEditorDialog(QDialog):
    imageImportNotice = Signal(str)

    def __init__(
        self,
        *,
        timezone: str,
        task: Task | None = None,
        reminder_rules: tuple[ReminderRuleInput, ...] = (),
        reminders: tuple[Reminder, ...] = (),
        deliveries: tuple[ReminderDelivery, ...] = (),
        on_save: Callable[[TaskEditorDialog], bool | None] | None = None,
        attachment_service: AttachmentService | None = None,
        initial_date: date | None = None,
        now: datetime | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._timezone = timezone
        self._task = task
        self.mutation_attempt = MutationAttempt()
        self._draft: TaskDraft | None = None
        self._on_save = on_save
        self._image_service = attachment_service
        self._saved_image_task: Task | None = None
        self.image_panel: ClipboardImagePanel | None = None
        self._image_controller = ImageImportController(self)
        self._image_controller.completed.connect(self._images_imported)
        self._deliveries = deliveries
        self._runtime: ReminderRuntimeState | None = None
        self._runtime_basis: tuple[object, ...] | None = None
        self._draft_basis: tuple[object, ...] | None = None
        self._reminder_edits: tuple[ReminderEdit, ...] = ()
        self._reminder_settings: ReminderSettingsDialog | None = None
        self._date_rows: dict[QWidget, QWidget] = {}
        self._custom_recurrence_rule: str | None = None
        self._default_alarm_untouched = task is None and not reminders and not reminder_rules
        self._end_autofill = task is None or task.ends_at is None
        self._syncing_end = False
        self._wheel_guard = WheelScrollGuard(self)
        self._date_review = DateReview(self)
        self._date_scroll_target: DateCaution | None = None
        self._date_scroll_timer = QTimer(self)
        self._date_scroll_timer.setSingleShot(True)
        self._date_scroll_timer.timeout.connect(self._show_date_caution)
        self._review_now = now
        self._reminder_date_baselines: dict[int, date] = {}

        self.setWindowTitle("업무 수정" if task else "새 업무")
        self.setModal(False)
        self.resize(520, 600)
        self.setMinimumSize(460, 520)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 12)
        scroll = QScrollArea()
        self._scroll = scroll
        scroll.setObjectName("taskEditorScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("taskEditorBody")
        root = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
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
        default_start = default_start_at((now or datetime.now(zone)).astimezone(zone))
        default_end = default_start + timedelta(hours=1)
        self.start_date_edit = QDateEdit(
            QDate(default_start.year, default_start.month, default_start.day)
        )
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.start_date_caution = DateCaution(
            date_with_weekday(self.start_date_edit), schedule_group
        )
        self._date_rows[self.start_date_edit] = self.start_date_caution
        self.schedule_form.addRow("시작일", self._date_rows[self.start_date_edit])

        self.start_time_input = TimeInput(QTime(default_start.hour, default_start.minute))
        self.start_time_edit = self.start_time_input.edit
        self._date_rows[self.start_time_edit] = self.start_time_input
        self.schedule_form.addRow("시작 시각", self.start_time_input)

        self.reminder_summary = WrappingLabel()
        self.reminder_settings_button = QPushButton("알림 설정…")
        self.reminder_settings_button.setObjectName("taskReminderSettings")
        self.reminder_settings_button.setMinimumWidth(108)
        reminder_container = QWidget(schedule_group)
        reminder_box = QVBoxLayout(reminder_container)
        reminder_box.setContentsMargins(0, 0, 0, 0)
        reminder_box.setSpacing(3)
        reminder_row = QWidget(reminder_container)
        reminder_layout = QHBoxLayout(reminder_row)
        reminder_layout.setContentsMargins(0, 0, 0, 0)
        reminder_layout.addWidget(self.reminder_summary, 1)
        reminder_layout.addWidget(self.reminder_settings_button)
        reminder_box.addWidget(reminder_row)
        self.reminder_notice = CompactLabel(parent=reminder_container)
        self.reminder_notice.setObjectName("reminderDateNotice")
        reminder_box.addWidget(self.reminder_notice)
        self.reminder_notice.hide()
        self.schedule_form.addRow("알림", reminder_container)
        self.reminder_settings_button.clicked.connect(self._open_reminder_settings)

        self.advanced_schedule_check = QCheckBox("종료·반복 등 추가 설정 보기")
        self.advanced_schedule_check.setObjectName("advancedScheduleCheck")
        self.schedule_form.addRow("", self.advanced_schedule_check)

        self.end_enabled_check = QCheckBox("종료 시각 지정")
        self.end_enabled_check.setObjectName("taskEndEnabledCheck")
        self.schedule_form.addRow("", self.end_enabled_check)

        self.end_date_edit = QDateEdit(QDate(default_end.year, default_end.month, default_end.day))
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.end_date_caution = DateCaution(date_with_weekday(self.end_date_edit), schedule_group)
        self._date_rows[self.end_date_edit] = self.end_date_caution
        self.schedule_form.addRow("종료일", self._date_rows[self.end_date_edit])

        self.end_time_input = TimeInput(QTime(default_end.hour, default_end.minute))
        self.end_time_edit = self.end_time_input.edit
        self._date_rows[self.end_time_edit] = self.end_time_input
        self.schedule_form.addRow("종료 시각", self.end_time_input)

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
        self.repeat_date_caution = DateCaution(
            date_with_weekday(self.repeat_until_date), schedule_group
        )
        self._date_rows[self.repeat_until_date] = self.repeat_date_caution
        self.schedule_form.addRow("반복 종료", self._date_rows[self.repeat_until_date])

        self._schedule_value_edits = (
            self.start_date_edit,
            self.start_time_edit,
            self.end_date_edit,
            self.end_time_edit,
            self.repeat_until_date,
        )
        for edit in self._schedule_value_edits:
            edit.installEventFilter(self)
            edit.installEventFilter(self._wheel_guard)
        for control in (
            self.priority_combo,
            self.status_combo,
            self.schedule_combo,
            self.repeat_combo,
            self.repeat_interval,
        ):
            control.installEventFilter(self._wheel_guard)
        root.addWidget(schedule_group)
        root.addStretch()

        if attachment_service is not None:
            self.image_panel = ClipboardImagePanel(self)
            outer.addWidget(self.image_panel)
        self.error_label = QLabel()
        self.error_label.setObjectName("formError")
        self.error_label.setStyleSheet("color: #C62828; font-weight: 600;")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        outer.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("저장")
        self.save_button.setObjectName("primaryButton")
        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setText("취소")
        for button in (self.save_button, self.cancel_button):
            button.setMinimumWidth(72)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        button_footer = QWidget(self)
        footer_layout = QHBoxLayout(button_footer)
        footer_layout.setContentsMargins(22, 0, 22, 0)
        footer_layout.addWidget(buttons)
        outer.addWidget(button_footer)

        self.schedule_combo.currentIndexChanged.connect(self._update_schedule_visibility)
        self.all_day_check.toggled.connect(self._update_schedule_visibility)
        self.advanced_schedule_check.toggled.connect(self._update_schedule_visibility)
        self.end_enabled_check.toggled.connect(self._update_schedule_visibility)
        self.start_date_edit.dateChanged.connect(self._keep_end_date_valid)
        self.repeat_combo.currentIndexChanged.connect(self._update_recurrence_visibility)
        self.repeat_until_check.toggled.connect(self._update_recurrence_visibility)

        if task is not None:
            self._populate(task)
        else:
            self.schedule_combo.setCurrentIndex(1)
            self.all_day_check.setChecked(False)
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
        if reminders:
            self._reminder_edits = tuple(
                ReminderEdit(
                    ReminderRuleInput(r.relation, r.offset_minutes, r.absolute_at, r.enabled), r.id
                )
                for r in reminders
            )
        elif task:
            self._reminder_edits = tuple(ReminderEdit(r) for r in reminder_rules)
        else:
            self._reminder_edits = (
                ReminderEdit(ReminderRuleInput(ReminderRelation.START, offset_minutes=0)),
            )
        self._draft_basis = self._alarm_signature(self._preview_task(), self.reminder_edits())
        self._initial_reminder_edits = self._reminder_edits
        self._refresh_reminder_summary()
        self._sync_end_default()
        self.start_time_edit.timeChanged.connect(self._sync_end_default)
        self.start_time_input.dayStepped.connect(
            lambda days: self.start_date_edit.setDate(self.start_date_edit.date().addDays(days))
        )
        self.end_time_input.dayStepped.connect(self._step_end_date)
        self.start_time_input.can_step_day = lambda days: self._date_step_allowed(
            self.start_date_edit, days
        )
        self.end_time_input.can_step_day = lambda days: self._date_step_allowed(
            self.end_date_edit, days
        )
        self.end_date_edit.dateChanged.connect(self._mark_end_edited)
        self.end_time_edit.timeChanged.connect(self._mark_end_edited)
        self.end_time_edit.textEdited.connect(self._mark_end_edited)
        self.end_enabled_check.toggled.connect(self._sync_end_default)
        for edit in (self.start_time_edit, self.end_time_edit):
            edit.invalidEntry.connect(
                lambda message, field=edit: self._show_entry_feedback(message, field)
            )
        for edit in self._schedule_value_edits:
            if isinstance(edit, QDateEdit):
                edit.dateChanged.connect(self._refresh_reminder_summary)
            elif isinstance(edit, WholeTimeEdit):
                edit.timeChanged.connect(self._refresh_reminder_summary)
        self.schedule_combo.currentIndexChanged.connect(self._refresh_reminder_summary)
        self.status_combo.currentIndexChanged.connect(self._refresh_reminder_summary)
        self.repeat_combo.currentIndexChanged.connect(self._refresh_reminder_summary)
        self.repeat_interval.valueChanged.connect(self._refresh_reminder_summary)
        for check in (self.all_day_check, self.end_enabled_check, self.repeat_until_check):
            check.toggled.connect(self._refresh_reminder_summary)
        self._configure_tab_order()
        self._date_baselines = {
            field: cast(date, field.date().toPython())
            for field in (self.start_date_edit, self.end_date_edit, self.repeat_until_date)
        }
        self._reminder_date_baselines = {
            id(edit): edit.rule.absolute_at.astimezone(zone).date()
            for edit in self._reminder_edits
            if edit.rule.absolute_at is not None
        }
        self._initial_schedule = (self._preview_task().starts_at, self._preview_task().ends_at)
        for field in self._schedule_value_edits:
            if isinstance(field, QDateEdit):
                field.dateChanged.connect(self._refresh_date_notice)
            elif isinstance(field, WholeTimeEdit):
                field.timeChanged.connect(self._refresh_date_notice)
        for check in (
            self.all_day_check,
            self.end_enabled_check,
            self.repeat_until_check,
            self.advanced_schedule_check,
        ):
            check.toggled.connect(self._refresh_date_notice)
        self.schedule_combo.currentIndexChanged.connect(self._refresh_date_notice)
        self.repeat_combo.currentIndexChanged.connect(self._refresh_date_notice)
        self._refresh_date_notice()
        self.title_edit.textEdited.connect(lambda _text: mark_input_error(self.title_edit, False))
        self.title_edit.setFocus()

    def _date_inputs(self) -> tuple[tuple[QDateEdit, DateInput], ...]:
        fields = (
            []
            if self.schedule_combo.currentData() == "none"
            else [("시작일", self.start_date_edit)]
        )
        if self.schedule_combo.currentData() == "range":
            fields.append(("종료일", self.end_date_edit))
        if fields and self.repeat_combo.currentData() and self.repeat_until_check.isChecked():
            fields.append(("반복 종료", self.repeat_until_date))
        return tuple(
            (
                field,
                DateInput(
                    name,
                    self._date_baselines[field],
                    cast(date, field.date().toPython()),
                    self._task is None,
                    name != "반복 종료",
                ),
            )
            for name, field in fields
        )

    def _reminder_date_inputs(self) -> tuple[DateInput, ...]:
        zone = ZoneInfo(self._timezone)
        today = (self._review_now or datetime.now(UTC)).astimezone(zone).date()
        return tuple(
            DateInput(
                "직접 지정 알림",
                self._reminder_date_baselines.get(id(edit), today),
                edit.rule.absolute_at.astimezone(zone).date(),
                edit.reminder_id is None,
            )
            for edit in self.reminder_edits()
            if edit.rule.enabled and edit.rule.absolute_at is not None
        )

    def _date_notices(self) -> tuple[InputNotice, ...]:
        today = (self._review_now or datetime.now(UTC)).astimezone(ZoneInfo(self._timezone)).date()
        return review_dates(
            (*(value for _field, value in self._date_inputs()), *self._reminder_date_inputs()),
            today=today,
        )

    def _refresh_date_notice(self) -> None:
        if not hasattr(self, "_date_baselines"):
            return
        today = (self._review_now or datetime.now(UTC)).astimezone(ZoneInfo(self._timezone)).date()
        active = dict(self._date_inputs())
        new_cautions = []
        for field, caution in (
            (self.start_date_edit, self.start_date_caution),
            (self.end_date_edit, self.end_date_caution),
            (self.repeat_until_date, self.repeat_date_caution),
        ):
            previous = caution.notice.full_text
            if field in active:
                caution.review(active[field], today)
            else:
                caution.set_warning("")
            if caution.notice.full_text and previous != caution.notice.full_text:
                new_cautions.append(caution)
        if new_cautions:
            self._date_scroll_target = next(
                (caution for caution in new_cautions if caution.edit.hasFocus()), new_cautions[0]
            )
            if self.isVisible():
                self._date_scroll_timer.start(0)
        alarm_notices = tuple(
            item
            for value in self._reminder_date_inputs()
            for item in review_dates((value,), today=today)
            if item.confirm or (value.is_new and abs((value.after - today).days) > 366)
        )
        notes = ["주의 · 직접 지정 알림 날짜 확인"] if alarm_notices else []
        details = [item.text for item in alarm_notices]
        if self.schedule_combo.currentData() != "none":
            try:
                draft = self._build_draft()
                count = sum(
                    edit.rule.enabled and edit.rule.absolute_at is not None
                    for edit in self.reminder_edits()
                )
                if count and (draft.starts_at, draft.ends_at) != self._initial_schedule:
                    notes.append(f"직접 지정 {count}개 · 시각 유지")
                    details.append(
                        f"직접 지정 알림 {count}개는 일정 날짜를 바꿔도 유지됩니다. 필요하면 알림 설정에서 변경하세요."
                    )
            except ValueError:
                notes.append("일정·시각 입력을 확인하세요")
        self.reminder_notice.setText(" · ".join(notes))
        self.reminder_notice.setToolTip("\n".join(details) or " · ".join(notes))
        self.reminder_notice.setVisible(bool(notes))
        flagged = bool(alarm_notices)
        if bool(self.reminder_settings_button.property("dateCaution")) != flagged:
            self.reminder_settings_button.setProperty("dateCaution", flagged)
            self.reminder_settings_button.style().unpolish(self.reminder_settings_button)
            self.reminder_settings_button.style().polish(self.reminder_settings_button)
            self.reminder_settings_button.update()
        if not self.advanced_schedule_check.isChecked():
            advanced = []
            if self.end_enabled_check.isChecked() and not self.all_day_check.isChecked():
                advanced.append("종료 " + self.end_time_edit.text())
            if self.repeat_combo.currentData():
                advanced.append(self.repeat_combo.currentText())
            if self.repeat_until_date.property("dateCaution"):
                advanced.append("날짜 확인")
            self.advanced_schedule_check.setText(
                "추가 설정 보기" + (" · " + " · ".join(advanced) if advanced else "")
            )

    def draft(self) -> TaskDraft:
        if self._draft is None:
            raise RuntimeError("대화상자가 아직 저장되지 않았습니다.")
        return self._draft

    def _show_date_caution(self) -> None:
        caution = self._date_scroll_target
        self._date_scroll_target = None
        if caution is not None and caution.isVisible() and not caution.notice.isHidden():
            self._scroll.ensureWidgetVisible(caution, 0, 8)

    def showEvent(self, event: QShowEvent) -> None:
        height = 40
        for button in (self.save_button, self.cancel_button):
            button.ensurePolished()
            height = max(height, button.sizeHint().height())
        for button in (self.save_button, self.cancel_button):
            button.setFixedHeight(height)
        for edit in (
            *self._schedule_value_edits,
            self.title_edit,
            self.priority_combo,
            self.status_combo,
            self.schedule_combo,
            self.repeat_combo,
            self.repeat_interval,
        ):
            edit.ensurePolished()
            edit.setMinimumHeight(edit.sizeHint().height())
        super().showEvent(event)
        if self._date_scroll_target is not None:
            self._date_scroll_timer.start(0)

    def reminder_edits(self) -> tuple[ReminderEdit, ...]:
        if self._can_suspend_default_alarm():
            return ()
        return self._reminder_edits

    def _can_suspend_default_alarm(self) -> bool:
        return (
            self._default_alarm_untouched
            and self.schedule_combo.currentData() == "none"
            and self._reminder_edits
            == (ReminderEdit(ReminderRuleInput(ReminderRelation.START, offset_minutes=0)),)
        )

    def update_runtime_state(self, state: ReminderRuntimeState) -> None:
        self._runtime = state
        if self._runtime_basis is None:
            self._runtime_basis = self._alarm_signature(state.task, self._runtime_edits(state))
        self._deliveries = state.deliveries
        if self._reminder_settings:
            self._reminder_settings.update_runtime_state(state)
        self._refresh_reminder_summary()

    def update_delivery_states(self, deliveries: tuple[ReminderDelivery, ...]) -> None:
        if deliveries == self._deliveries:
            return
        self._deliveries = deliveries
        if self._reminder_settings:
            self._reminder_settings.update_delivery_states(deliveries)
        self._refresh_reminder_summary()

    def _preview_task(self) -> Task:
        draft = self._build_draft()
        return Task.create(
            title=draft.title or "새 업무",
            status=draft.status,
            starts_at=draft.starts_at,
            ends_at=draft.ends_at,
            all_day=draft.all_day,
            timezone=draft.timezone,
            recurrence_rule=draft.recurrence_rule,
        )

    @staticmethod
    def _runtime_edits(actual: ReminderRuntimeState) -> tuple[ReminderEdit, ...]:
        return tuple(
            ReminderEdit(
                ReminderRuleInput(r.relation, r.offset_minutes, r.absolute_at, r.enabled), r.id
            )
            for r in actual.reminders
        )

    @staticmethod
    def _alarm_signature(task: Task, edits: tuple[ReminderEdit, ...]) -> tuple[object, ...]:
        rules = tuple(
            sorted(
                (
                    (
                        edit.reminder_id,
                        edit.rule.relation.value,
                        edit.rule.offset_minutes,
                        edit.rule.absolute_at.astimezone(UTC) if edit.rule.absolute_at else None,
                        edit.rule.enabled,
                    )
                    for edit in edits
                ),
                key=repr,
            )
        )
        return (
            task.starts_at,
            task.ends_at,
            task.all_day,
            task.timezone,
            task.recurrence_rule,
            task.status,
            task.deleted_at,
            rules,
        )

    def _refresh_reminder_summary(self) -> None:
        now = datetime.now(UTC)
        actual = self._runtime
        execution = (
            reminder_summary(
                self._runtime_edits(actual),
                actual.task,
                now,
                actual.deliveries,
                actual,
                compact=True,
            )
            if actual
            else ""
        )
        try:
            task = self._preview_task()
            preview = reminder_summary(self.reminder_edits(), task, now, compact=True)
            if actual:
                draft_signature = self._alarm_signature(task, self.reminder_edits())
                actual_signature = self._alarm_signature(actual.task, self._runtime_edits(actual))
                local_change = (
                    self._draft_basis is not None and draft_signature != self._draft_basis
                )
                notices = []
                if local_change and draft_signature != actual_signature:
                    notices.append("저장 전 변경 있음 · 저장 후 적용")
                elif self._runtime_basis is not None and actual_signature != self._runtime_basis:
                    notices.append("실제 상태 갱신됨")
                text = execution + ("\n" + " · ".join(notices) if notices else "")
                tooltip = "실제 예약:\n" + execution + "\n\n저장 전 예상:\n" + preview
            else:
                text = ("저장 전 · " if self._task is None else "설정 미리보기 · ") + preview
                if self._task is None:
                    text = text.replace(" · 다음 ", " · 예상 ")
                tooltip = text + "\n편집창에서 최종 저장해야 예약에 적용됩니다."
            self.reminder_summary.setText(text)
            self.reminder_summary.setToolTip(tooltip)
            if self._reminder_settings:
                self._reminder_settings.update_basis(task)
        except ValueError:
            self.reminder_summary.setText(
                (execution + "\n" if execution else f"알림 {len(self._reminder_edits)}개 · ")
                + "입력 확인 필요"
            )
            self.reminder_summary.setToolTip(
                "입력한 일정·시각을 확인하세요. 저장된 실제 예약은 변경되지 않았습니다."
            )

    def _open_reminder_settings(self) -> None:
        if self._reminder_settings:
            self._reminder_settings.raise_()
            return
        try:
            dialog = ReminderSettingsDialog(
                self._preview_task(),
                self._reminder_edits,
                deliveries=self._deliveries,
                date_baselines=self._reminder_date_baselines,
                parent=self,
            )
        except ValueError as error:
            self.error_label.setText(str(error))
            self.error_label.show()
            return
        self._reminder_settings = dialog
        if self._runtime:
            dialog.update_runtime_state(self._runtime)
        self.save_button.setEnabled(False)

        def finished(result: int) -> None:
            if result == QDialog.DialogCode.Accepted:
                self._reminder_edits = dialog.edits()
                self._reminder_date_baselines = dialog.date_baselines()
                self._default_alarm_untouched = False
            self._reminder_settings = None
            self.save_button.setEnabled(True)
            self._refresh_reminder_summary()
            self._refresh_date_notice()
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.show()

    def done(self, result: int) -> None:
        self._date_scroll_timer.stop()
        self._date_scroll_target = None
        self._date_review.close()
        if self._reminder_settings:
            self._reminder_settings.reject()
        self._image_controller.shutdown()
        if self.image_panel:
            self.image_panel.shutdown()
        super().done(result)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched in self._schedule_value_edits and event.type() is QEvent.Type.KeyPress:
            key_event = cast(QKeyEvent, event)
            if isinstance(watched, WholeTimeEdit) and key_event.key() in (
                Qt.Key.Key_Tab,
                Qt.Key.Key_Backtab,
            ):
                try:
                    watched.commit()
                except ValueError as error:
                    self._show_input_error(str(error), watched)
                    return True
            if key_event.key() == Qt.Key.Key_Tab:
                return self.focusNextPrevChild(True)
            if key_event.key() == Qt.Key.Key_Backtab:
                return self.focusNextPrevChild(False)
        return super().eventFilter(watched, event)

    def _validate_and_accept(self) -> None:
        if self._image_controller.busy:
            return
        if self._saved_image_task is not None:
            try:
                if self._on_save and self._on_save(self) is False:
                    return
            except Exception as error:
                self._show_input_error(str(error), None)
                return
            self.accept()
            return
        self._clear_input_errors()
        try:
            if self.image_panel:
                self.image_panel.entries()
            if not self.title_edit.text().strip():
                self._show_input_error("업무 제목을 입력하세요.", self.title_edit)
                return
            self._validate_time_inputs()
            self._draft = self._build_draft()
            if (
                self._draft.ends_at is not None
                and self._draft.starts_at is not None
                and self._draft.ends_at <= self._draft.starts_at
            ):
                field = (
                    self.end_date_edit
                    if self.end_date_edit.date() < self.start_date_edit.date()
                    else self.end_time_edit
                )
                self._show_input_error(
                    "종료 일정은 시작 일정보다 뒤여야 합니다. 다음 날 종료라면 기간 일정을 선택하세요.",
                    field,
                )
                return
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
            for edit in self.reminder_edits():
                if edit.rule.relation is ReminderRelation.START and self._draft.starts_at is None:
                    self._show_input_error(
                        "시작 기준 알림을 삭제하거나 날짜 직접 지정으로 변경하세요.",
                        self.reminder_settings_button,
                    )
                    return
                if edit.rule.relation is ReminderRelation.END and self._draft.ends_at is None:
                    self._show_input_error(
                        "종료 기준 알림을 삭제하거나 날짜 직접 지정으로 변경하세요.",
                        self.reminder_settings_button,
                    )
                    return
            notices = self._date_notices()
            if any(notice.confirm for notice in notices) and self._draft.status in {
                TaskStatus.ACTIVE,
                TaskStatus.PENDING,
            }:
                preview = self._preview_task()
                for edit in self.reminder_edits():
                    if not edit.rule.enabled:
                        continue
                    unchanged_rule = edit in self._initial_reminder_edits
                    unchanged_basis = self._task is not None and (
                        edit.rule.relation is ReminderRelation.ABSOLUTE
                        or (preview.starts_at, preview.ends_at, preview.recurrence_rule)
                        == (self._task.starts_at, self._task.ends_at, self._task.recurrence_rule)
                    )
                    if (
                        not (unchanged_rule and unchanged_basis)
                        and candidate(edit, preview, self._review_now or datetime.now(UTC)) is None
                    ):
                        raise ValueError("새 알림은 현재 분 이후로 지정하세요.")
            if not self._date_review.allow(
                (self._draft, self.reminder_edits()),
                notices,
                self._validate_and_accept,
            ):
                return
            if self._on_save and self._on_save(self) is False:
                return
        except Exception as error:
            error_field = (
                self.repeat_until_date
                if "반복 종료일" in str(error)
                else self.end_date_edit
                if "종료일" in str(error)
                else self.repeat_combo
                if "반복" in str(error)
                else self.start_time_edit
                if "새 알림" in str(error) and self._default_alarm_untouched
                else self.reminder_settings_button
                if "새 알림" in str(error)
                else None
            )
            self._show_input_error(str(error), error_field)
            return
        self.accept()

    def start_image_import(self, saved: Task) -> bool:
        if self.image_panel is None or not self.image_panel.has_images:
            return True
        if self._image_service is None or saved.id is None:
            raise ValueError("사진 첨부 기능을 사용할 수 없습니다.")
        entries = self.image_panel.entries()
        self._saved_image_task = saved
        body = self._scroll.widget()
        if body:
            body.setEnabled(False)
        self.image_panel.setEnabled(False)
        self.save_button.setEnabled(False)
        self.cancel_button.setText("닫기")
        self.cancel_button.setToolTip("업무는 이미 저장됐습니다. 닫으면 미처리 사진은 버립니다.")
        self.error_label.setText("업무는 저장됐습니다. 사진을 첨부하고 있습니다…")
        self.error_label.show()
        self._image_controller.start(self._image_service, saved.id, entries)
        return False

    def _images_imported(self, result: ImageImportResult) -> None:
        if self.image_panel is None:
            return
        self.image_panel.remove_completed(result.completed)
        if result.completed:
            self.imageImportNotice.emit(result.notice)
        self.image_panel.setEnabled(True)
        self.save_button.setEnabled(True)
        if self.image_panel.has_images:
            self.save_button.setText("첨부 다시 시도")
            self.error_label.setText(
                "업무는 이미 저장됐습니다. 미처리 사진만 다시 첨부하세요.\n" + result.error_summary
            )
            self.error_label.setToolTip("\n".join(result.errors))
            self.error_label.show()
        else:
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
        reminder_rules = tuple(edit.rule for edit in self.reminder_edits())
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
        self._set_row_visible(self.advanced_schedule_check, has_schedule)
        self.advanced_schedule_check.setText(
            "종료·반복 등 추가 설정 접기" if advanced else "종료·반복 등 추가 설정 보기"
        )
        target_height = 720 if advanced else 600
        if self.screen():
            target_height = min(
                target_height,
                max(self.minimumHeight(), self.screen().availableGeometry().height() - 60),
            )
        if self.height() != target_height:
            self.resize(self.width(), target_height)
        self._set_row_visible(self.end_enabled_check, advanced and is_timed and not is_range)
        self._set_row_visible(self.end_date_edit, advanced and is_range)
        self._set_row_visible(self.end_time_edit, advanced and is_timed and has_end)
        self._set_row_visible(self.repeat_combo, advanced)
        self.end_time_input.allow_day_step = is_range
        self._sync_end_default()
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
        row = self._date_rows.get(field, field)
        label = self.schedule_form.labelForField(row)
        if label is not None:
            label.setVisible(visible)
        row.setVisible(visible)
        field.setVisible(visible)

    def _keep_end_date_valid(self, start_date: QDate) -> None:
        self._sync_end_default()
        if (
            self._task is None
            and not self.repeat_until_check.isChecked()
            and self.repeat_until_date.date() < start_date
        ):
            self.repeat_until_date.setDate(start_date.addDays(30))

    def _sync_end_default(self, *_args: object) -> None:
        if not self._end_autofill or self._syncing_end:
            return
        try:
            start = cast(date, self.start_date_edit.date().toPython())
            value = self.start_time_edit.time()
        except ValueError:
            return
        end = datetime.combine(start, time(value.hour(), value.minute())) + timedelta(hours=1)
        if self.schedule_combo.currentData() != "range" and end.date() != start:
            end = datetime.combine(start, time(23, 59))
        self._syncing_end = True
        try:
            self.end_date_edit.setDate(QDate(end.year, end.month, end.day))
            self.end_time_edit.setTime(QTime(end.hour, end.minute))
        finally:
            self._syncing_end = False

    def _mark_end_edited(self, *_args: object) -> None:
        if not self._syncing_end:
            self._end_autofill = False

    def _step_end_date(self, days: int) -> None:
        if self.schedule_combo.currentData() != "range":
            # A day schedule cannot encode an end on another day.
            self._show_input_error("다음 날 종료는 기간 일정을 선택하세요.", self.schedule_combo)
            return
        self.end_date_edit.setDate(self.end_date_edit.date().addDays(days))

    @staticmethod
    def _date_step_allowed(edit: QDateEdit, days: int) -> bool:
        candidate = edit.date().addDays(days)
        return candidate.isValid() and edit.minimumDate() <= candidate <= edit.maximumDate()

    def _validate_time_inputs(self) -> None:
        if self.schedule_combo.currentData() == "none" or self.all_day_check.isChecked():
            return
        fields = [self.start_time_edit]
        if self.schedule_combo.currentData() == "range" or self.end_enabled_check.isChecked():
            fields.append(self.end_time_edit)
        for field in fields:
            try:
                field.commit()
            except ValueError as error:
                self._show_input_error(str(error), field)
                raise

    def _clear_input_errors(self) -> None:
        self.error_label.hide()
        for field in (
            *self._schedule_value_edits,
            self.title_edit,
            self.schedule_combo,
            self.reminder_settings_button,
            self.repeat_combo,
        ):
            mark_input_error(field, False)

    def _show_input_error(self, message: str, field: QWidget | None = None) -> None:
        self.error_label.setText(message)
        self.error_label.show()
        if field is not None:
            if field in (
                self.end_date_edit,
                self.end_time_edit,
                self.repeat_combo,
                self.repeat_until_date,
            ):
                self.advanced_schedule_check.setChecked(True)
            mark_input_error(field, True)
            self._scroll.ensureWidgetVisible(field)
            field.setFocus()

    def _show_entry_feedback(self, message: str, field: QWidget) -> None:
        # Focus-out can occur inside visibility changes. Don't recursively reopen
        # or refocus controls while their parent is hiding them; Save/Tab focus errors.
        self.error_label.setText(message)
        self.error_label.show()
        mark_input_error(field, True)

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
            self.reminder_settings_button,
            self.advanced_schedule_check,
            self.end_enabled_check,
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
