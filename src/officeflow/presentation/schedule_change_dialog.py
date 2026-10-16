"""Small modeless date movement; persistence remains an application command."""

from collections.abc import Callable
from datetime import date, datetime, time
from typing import cast
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, Qt, QTime
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.input_review import DateInput, review_dates
from officeflow.application.schedule_change import moved_schedule
from officeflow.presentation.dates import date_with_weekday, format_datetime
from officeflow.presentation.input_controls import WheelScrollGuard, WholeTimeEdit
from officeflow.presentation.input_review import DateCaution, DateReview
from officeflow.presentation.theme import LIGHT_STYLESHEET


class ScheduleChangeDialog(QDialog):
    def __init__(
        self,
        *,
        title: str,
        starts_at: datetime | None,
        ends_at: datetime | None,
        all_day: bool,
        timezone: str,
        target: date,
        today: date,
        recurring: bool,
        absolute_count: int,
        save: Callable[[datetime, datetime | None, bool], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("날짜·시간 변경 · OfficeFlow")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setStyleSheet(LIGHT_STYLESHEET)
        self.resize(460, 300)
        self._start, self._end, self._all_day = starts_at, ends_at, all_day
        self._timezone, self._today, self._save = timezone, today, save
        self._before_day = starts_at.astimezone(ZoneInfo(timezone)).date() if starts_at else today
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setWordWrap(True)
        layout.addWidget(heading)
        self.before = QLabel(
            "기존: " + (format_datetime(starts_at, timezone) if starts_at else "일정 없음")
        )
        layout.addWidget(self.before)
        if recurring:
            scope = QLabel("선택 회차만 이동 · 이미 울린 알림·재알림은 종료")
            scope.setWordWrap(True)
            scope.setToolTip(
                "다른 회차와 반복 규칙은 유지합니다. 아직 울리지 않은 시작·종료 기준 알림은 이동한 일정을 따릅니다."
            )
            layout.addWidget(scope)
        form = QFormLayout()
        self.date_edit = date_with_weekday(QDateEdit(QDate(target.year, target.month, target.day)))
        self.date_edit.setCalendarPopup(True)
        self._wheel_guard = WheelScrollGuard(self)
        self.date_edit.installEventFilter(self._wheel_guard)
        self.caution = DateCaution(self.date_edit)
        form.addRow("이동 날짜", self.caution)
        local = starts_at.astimezone(ZoneInfo(timezone)) if starts_at else None
        self.time_edit = WholeTimeEdit(QTime(local.hour, local.minute) if local else QTime(9, 0))
        self.timed = QCheckBox("시각 지정")
        self.timed.setChecked(starts_at is not None and not all_day)
        self.timed.setVisible(starts_at is None)
        form.addRow("", self.timed)
        form.addRow("시각", self.time_edit)
        self.time_edit.setVisible(self.timed.isChecked())
        form.labelForField(self.time_edit).setVisible(self.timed.isChecked())

        def toggle(checked: bool) -> None:
            self.time_edit.setVisible(checked)
            form.labelForField(self.time_edit).setVisible(checked)

        self.timed.toggled.connect(toggle)
        layout.addLayout(form)
        self.after = QLabel()
        self.after.setWordWrap(True)
        layout.addWidget(self.after)
        if absolute_count:
            warning = QLabel(
                f"날짜 지정 알림 {absolute_count}개는 이동하지 않습니다. 시작·종료 기준 알림만 함께 이동합니다."
            )
            warning.setWordWrap(True)
            layout.addWidget(warning)
        self.error = QLabel()
        self.error.setObjectName("dateCautionNotice")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.review = DateReview(self)
        self.finished.connect(lambda _result: self.review.close())
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("이동")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.date_edit.dateChanged.connect(self._preview)
        self.time_edit.textChanged.connect(self._preview)
        self.timed.toggled.connect(self._preview)
        self._preview()

    def _date_input(self) -> DateInput:
        return DateInput(
            "일정",
            self._before_day,
            cast(date, self.date_edit.date().toPython()),
            is_new=self._start is None,
        )

    def values(self) -> tuple[datetime, datetime | None, bool]:
        all_day = not self.timed.isChecked() if self._start is None else self._all_day
        at = cast(time, self.time_edit.time().toPython()) if self.timed.isChecked() else None
        start, end = moved_schedule(
            self._start,
            self._end,
            target=cast(date, self.date_edit.date().toPython()),
            timezone=self._timezone,
            all_day=all_day,
            at=at,
        )
        return start, end, all_day

    def _preview(self, *_args: object) -> None:
        self.caution.review(self._date_input(), self._today)
        try:
            start, end, all_day = self.values()
            self.after.setText(
                "이동 후: "
                + format_datetime(start, self._timezone)
                + (" · 종일" if all_day else "")
                + ("\n종료: " + format_datetime(end, self._timezone) if end else "")
            )
            self.error.clear()
        except ValueError as error:
            self.error.setText(str(error))

    def _submit(self) -> None:
        try:
            values = self.values()
            if not self.review.allow(
                values, review_dates((self._date_input(),), today=self._today), self._submit
            ):
                return
            self._save(*values)
        except Exception as error:
            self.error.setText(str(error))
            return
        self.accept()
