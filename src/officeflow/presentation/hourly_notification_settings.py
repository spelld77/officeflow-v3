from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, QTime, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.hourly_notifications import HourlyState
from officeflow.domain.hourly_notification import HourlySettings
from officeflow.presentation.compact_labels import WrappingLabel
from officeflow.presentation.dates import format_date
from officeflow.presentation.detail_widgets import CollapsibleText
from officeflow.presentation.lookup_style import SCROLLBAR_STYLE
from officeflow.presentation.theme import LIGHT_STYLESHEET


class HourlyNotificationSettingsDialog(QDialog):
    saveRequested = Signal(object)
    holidayRequested = Signal(bool)
    morningRequested = Signal()

    def __init__(self, settings: HourlySettings) -> None:
        super().__init__(None)
        self.setWindowTitle("매시 알림 설정")
        self.setModal(False)
        self.setStyleSheet(LIGHT_STYLESHEET)
        self.resize(510, 530)
        self.setMinimumSize(430, 420)
        self._settings = settings
        root = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        scroll.setStyleSheet(SCROLLBAR_STYLE)
        scroll.viewport().setStyleSheet("background:#F4F6FA;color:#172033;")
        body = QWidget()
        body.setStyleSheet("background: #F4F6FA;color:#172033;")
        content = QVBoxLayout(body)
        self.next_label = WrappingLabel()
        self.next_label.setText("현재 상태 확인 중…")
        content.addWidget(self.next_label)
        self.draft_label = WrappingLabel()
        self.draft_label.setText("설정 변경 · 저장 전")
        self.draft_label.setObjectName("mutedText")
        self.draft_label.hide()
        content.addWidget(self.draft_label)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.message_edit = QLineEdit(settings.message)
        self.message_edit.setMaxLength(200)
        form.addRow("알림 문구", self.message_edit)
        self.minute_spin = QSpinBox()
        self.minute_spin.setRange(0, 59)
        self.minute_spin.setSuffix("분")
        self.minute_spin.setValue(settings.minute)
        form.addRow("알림 시점 · 매시", self.minute_spin)
        self.weekday_start = self._time_edit(settings.weekday_exclusion_start)
        self.weekday_end = self._time_edit(settings.weekday_exclusion_end)
        row = QHBoxLayout()
        row.addWidget(self.weekday_start)
        row.addWidget(QLabel("~"))
        row.addWidget(self.weekday_end)
        form.addRow("평일 제외 시간", row)
        content.addLayout(form)
        self.schedule_explanation = QLabel(
            "토·일은 하루 전체 대상입니다. 추가 제외 시간은 적용됩니다."
        )
        self.schedule_explanation.setWordWrap(True)
        content.addWidget(self.schedule_explanation)
        self.today_label = WrappingLabel()
        content.addWidget(self.today_label)
        self.holiday_check = QCheckBox("오늘 휴일근무")
        self.holiday_check.clicked.connect(lambda checked: self.holidayRequested.emit(checked))
        content.addWidget(self.holiday_check)
        self.morning_button = QPushButton("오늘 08시 알림 받기")
        self.morning_button.setToolTip("08시 전에 인사랑 출근·업무기록을 했는데 앱을 늦게 켠 경우")
        self.morning_button.clicked.connect(self.morningRequested.emit)
        content.addWidget(self.morning_button)
        self.morning_resume_hint = QLabel(
            "08시 전 출근·기록 후, 프로그램만 늦게 켠 경우에 선택하세요."
        )
        self.morning_resume_hint.setWordWrap(True)
        self.morning_resume_hint.setObjectName("mutedText")
        content.addWidget(self.morning_resume_hint)
        toggle = QPushButton("추가 설정 보기")
        self.advanced_toggle = toggle
        toggle.setCheckable(True)
        content.addWidget(toggle)
        advanced = QWidget()
        advanced_layout = QVBoxLayout(advanced)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        self.sound_check = QCheckBox("새 알림 / 다시 알림 시 소리")
        self.sound_check.setChecked(settings.sound)
        advanced_layout.addWidget(self.sound_check)
        self.morning_check = QCheckBox("평일 08시: 늦게 켜도 알림 받기")
        # Positive wording: checked means opt out of the default late-start exclusion.
        self.morning_check.setChecked(not settings.morning_continuation_required)
        advanced_layout.addWidget(self.morning_check)
        self.morning_option_hint = QLabel(
            "기본 끔 · 08시 전부터 켜두었다면 08시도 이어서 알립니다."
        )
        self.morning_option_hint.setWordWrap(True)
        self.morning_option_hint.setObjectName("mutedText")
        advanced_layout.addWidget(self.morning_option_hint)
        hint = QLabel("모든 요일의 추가 제외 시간 · 최대 8개\n예: 23:00 ~ 06:00 (자정 넘김 가능)")
        hint.setWordWrap(True)
        advanced_layout.addWidget(hint)
        self._ranges: list[tuple[QWidget, QTimeEdit, QTimeEdit]] = []
        self._ranges_layout = QVBoxLayout()
        advanced_layout.addLayout(self._ranges_layout)
        self.add_range_button = QPushButton("제외 시간 추가")
        self.add_range_button.clicked.connect(lambda: self._add_range("12:00", "13:00"))
        advanced_layout.addWidget(self.add_range_button)
        for start, end in settings.extra_exclusions:
            self._add_range(start, end)
        advanced.hide()
        toggle.toggled.connect(advanced.setVisible)
        toggle.toggled.connect(
            lambda checked: toggle.setText("추가 설정 접기" if checked else "추가 설정 보기")
        )
        content.addWidget(advanced)
        self.behavior_help = CollapsibleText(
            "동작 설명",
            "평일에는 설정한 제외 시간에 알리지 않습니다. 토·일과 오늘 휴일근무는 평일 제외를 적용하지 않지만 추가 제외 시간은 적용합니다.\n"
            "평일 08시: 기본은 08시 이전부터 켜둔 경우에만 이어서 알립니다. 05시부터 또는 전날부터 켜두었다면 08시도 대상입니다. 처음 08:30에 켜면 생략합니다.\n"
            "08시 이전 출근·기록 후 앱만 늦게 켰다면 오늘 08시 알림 받기를 선택하세요. 출근 시각을 추정하거나 인사랑 기록을 자동 작성하지 않습니다.\n"
            "늦게 켜도 받기를 켜면 평일 08:00~08:59 최초 실행도 대상입니다. 다른 제외 시간·끄기·해당 시간대 처리 조건은 그대로 적용합니다.\n"
            "오늘 선택은 즉시 적용하며 같은 날짜의 재실행에도 유지됩니다. 다음 날 만료되고 오늘 08시 허용은 09시 이후 만료됩니다.\n"
            "끄기는 이번 실행에만 적용합니다. 완전히 종료한 뒤 다시 켜면 알림이 켜집니다. 설정과 오늘 예외 전체가 초기화되는 것은 아닙니다.\n"
            "문구·분·제외 시간·추가 옵션은 저장해야 적용합니다. 대상 시간대에 시작하면 현재 시간대 안내를 바로 표시합니다.",
        )
        content.addWidget(self.behavior_help)
        self.warning_label = QLabel("59분은 기록할 여유가 짧고 표시가 지연될 수 있습니다.")
        self.warning_label.setWordWrap(True)
        self.warning_label.setVisible(settings.minute == 59)
        self.minute_spin.valueChanged.connect(
            lambda value: self.warning_label.setVisible(value == 59)
        )
        content.addWidget(self.warning_label)
        content.addStretch()
        scroll.setWidget(body)
        root.addWidget(scroll)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #C62828;")
        root.addWidget(self.error_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.close)
        root.addWidget(buttons)
        for edit in (
            self.message_edit,
            self.minute_spin,
            self.weekday_start,
            self.weekday_end,
            self.sound_check,
            self.morning_check,
        ):
            if isinstance(edit, QLineEdit):
                edit.textChanged.connect(self._update_draft)
            elif isinstance(edit, QSpinBox):
                edit.valueChanged.connect(self._update_draft)
            elif isinstance(edit, QTimeEdit):
                edit.timeChanged.connect(self._update_draft)
            else:
                edit.toggled.connect(self._update_draft)
        self._update_draft()

    @staticmethod
    def _time_edit(value: str) -> QTimeEdit:
        edit = QTimeEdit(QTime.fromString(value, "HH:mm"))
        edit.setDisplayFormat("HH:mm")
        return edit

    def _draft_settings(self) -> HourlySettings:
        return replace(
            self._settings,
            message=self.message_edit.text().strip(),
            minute=self.minute_spin.value(),
            weekday_exclusion_start=self.weekday_start.time().toString("HH:mm"),
            weekday_exclusion_end=self.weekday_end.time().toString("HH:mm"),
            extra_exclusions=tuple(
                (a.time().toString("HH:mm"), b.time().toString("HH:mm")) for _, a, b in self._ranges
            ),
            sound=self.sound_check.isChecked(),
            morning_continuation_required=not self.morning_check.isChecked(),
        )

    def _update_draft(self) -> None:
        try:
            dirty = self._draft_settings() != self._settings
        except ValueError:
            dirty = True
        self.draft_label.setVisible(dirty)

    def mark_saved(self, settings: HourlySettings) -> None:
        self._settings = settings
        self._update_draft()
        self.error_label.setStyleSheet("color:#2E7D5B;")
        self.error_label.setText("설정을 저장했습니다.")

    def _add_range(self, start: str, end: str) -> None:
        if len(self._ranges) >= 8:
            return
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        a, b = self._time_edit(start), self._time_edit(end)
        remove = QPushButton("삭제")
        remove.clicked.connect(lambda: self._remove_range(row))
        layout.addWidget(a)
        layout.addWidget(QLabel("~"))
        layout.addWidget(b)
        layout.addWidget(remove)
        self._ranges.append((row, a, b))
        a.timeChanged.connect(self._update_draft)
        b.timeChanged.connect(self._update_draft)
        self._ranges_layout.addWidget(row)
        self.add_range_button.setEnabled(len(self._ranges) < 8)
        self._update_draft()

    def _remove_range(self, row: QWidget) -> None:
        self._ranges = [item for item in self._ranges if item[0] is not row]
        row.deleteLater()
        self.add_range_button.setEnabled(True)
        self._update_draft()

    def _save(self) -> None:
        try:
            updated = self._draft_settings()
        except ValueError as error:
            self.error_label.setStyleSheet("color:#C62828;")
            self.error_label.setText(str(error))
            return
        self.saveRequested.emit(updated)

    def update_runtime(self, state: HourlyState, next_text: str) -> None:
        self.holiday_check.setChecked(state.holiday_today)
        self.holiday_check.setEnabled(not state.weekend)
        self.holiday_check.setToolTip(
            "주말은 기본 하루 전체" if state.weekend else "오늘 날짜에만 적용"
        )
        self.morning_button.setVisible(state.can_resume_morning)
        self.morning_resume_hint.setVisible(state.can_resume_morning)
        self.today_label.setText(f"오늘만 바로 적용 · {format_date(state.bucket.starts_at.date())}")
        self.holiday_check.setText("주말 · 평일 제외 미적용" if state.weekend else "오늘 휴일근무")
        self.next_label.setText(f"현재: {'켜짐' if state.enabled else '꺼짐'} · {next_text}")
