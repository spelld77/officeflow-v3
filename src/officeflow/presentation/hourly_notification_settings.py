from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QTime, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
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
        body = QWidget()
        body.setStyleSheet("background: #F4F6FA;")
        content = QVBoxLayout(body)
        form = QFormLayout()
        self.message_edit = QLineEdit(settings.message)
        self.message_edit.setMaxLength(200)
        form.addRow("알림 문구", self.message_edit)
        self.minute_spin = QSpinBox()
        self.minute_spin.setRange(0, 59)
        self.minute_spin.setSuffix("분")
        self.minute_spin.setValue(settings.minute)
        form.addRow("매시 알림", self.minute_spin)
        self.weekday_start = self._time_edit(settings.weekday_exclusion_start)
        self.weekday_end = self._time_edit(settings.weekday_exclusion_end)
        row = QHBoxLayout()
        row.addWidget(self.weekday_start)
        row.addWidget(QLabel("~"))
        row.addWidget(self.weekday_end)
        form.addRow("평일 제외 시간", row)
        content.addLayout(form)
        self.schedule_explanation = QLabel()
        self.schedule_explanation.setWordWrap(True)
        content.addWidget(self.schedule_explanation)
        self.holiday_check = QCheckBox("오늘 휴일근무 (평일도 하루 전체 알림)")
        self.holiday_check.clicked.connect(lambda checked: self.holidayRequested.emit(checked))
        content.addWidget(self.holiday_check)
        self.morning_button = QPushButton("오늘 08시 알림 받기")
        self.morning_button.setToolTip("08시 전에 인사랑 출근·업무기록을 했는데 앱을 늦게 켠 경우")
        self.morning_button.clicked.connect(self.morningRequested.emit)
        content.addWidget(self.morning_button)
        self.morning_resume_hint = QLabel("08시 전 출근·기록 후, 프로그램만 늦게 켠 경우에 선택하세요.")
        self.morning_resume_hint.setWordWrap(True)
        self.morning_resume_hint.setObjectName("mutedText")
        content.addWidget(self.morning_resume_hint)
        self.next_label = QLabel()
        self.next_label.setWordWrap(True)
        content.addWidget(self.next_label)
        toggle = QPushButton("추가 설정 보기")
        self.advanced_toggle = toggle
        toggle.setCheckable(True)
        content.addWidget(toggle)
        advanced = QWidget()
        advanced_layout = QVBoxLayout(advanced)
        self.sound_check = QCheckBox("새 알림 / 다시 알림 시 소리")
        self.sound_check.setChecked(settings.sound)
        advanced_layout.addWidget(self.sound_check)
        self.morning_check = QCheckBox("늦게 켜도 08시 알림 받기")
        # Positive wording: checked means opt out of the default late-start exclusion.
        self.morning_check.setChecked(not settings.morning_continuation_required)
        advanced_layout.addWidget(self.morning_check)
        self.morning_option_hint = QLabel(
            "켜면 평일 08:00~08:59에 처음 실행해도 알립니다.\n"
            "인사랑용 기본은 끔입니다. 08시 전부터 켜두었다면 선택하지 않아도 이어서 알립니다."
        )
        self.morning_option_hint.setWordWrap(True)
        self.morning_option_hint.setObjectName("mutedText")
        advanced_layout.addWidget(self.morning_option_hint)
        self.morning_check.toggled.connect(self._update_schedule_explanation)
        self._update_schedule_explanation()
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
        caption = QLabel(
            "끄기는 이번 실행에만 적용됩니다.\n오늘 휴일근무 / 오늘 08시 알림은 선택 즉시 적용됩니다."
        )
        caption.setWordWrap(True)
        root.addWidget(caption)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.close)
        root.addWidget(buttons)

    @staticmethod
    def _time_edit(value: str) -> QTimeEdit:
        edit = QTimeEdit(QTime.fromString(value, "HH:mm"))
        edit.setDisplayFormat("HH:mm")
        return edit

    def _update_schedule_explanation(self) -> None:
        if self.morning_check.isChecked():
            morning = "평일 08시 이후 처음 켜도 아침 알림을 받습니다."
        else:
            morning = (
                "평일 08시 이후 처음 켜면 아침 알림을 생략합니다.\n"
                "08시 전부터 켜두면 08시에도 자동으로 알립니다."
            )
        self.schedule_explanation.setText(f"토·일은 하루 전체 알림 대상입니다.\n{morning}")

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
        self._ranges_layout.addWidget(row)
        self.add_range_button.setEnabled(len(self._ranges) < 8)

    def _remove_range(self, row: QWidget) -> None:
        self._ranges = [item for item in self._ranges if item[0] is not row]
        row.deleteLater()
        self.add_range_button.setEnabled(True)

    def _save(self) -> None:
        try:
            updated = replace(
                self._settings,
                message=self.message_edit.text().strip(),
                minute=self.minute_spin.value(),
                weekday_exclusion_start=self.weekday_start.time().toString("HH:mm"),
                weekday_exclusion_end=self.weekday_end.time().toString("HH:mm"),
                extra_exclusions=tuple(
                    (a.time().toString("HH:mm"), b.time().toString("HH:mm"))
                    for _, a, b in self._ranges
                ),
                sound=self.sound_check.isChecked(),
                morning_continuation_required=not self.morning_check.isChecked(),
            )
        except ValueError as error:
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
        self.next_label.setText(next_text)
