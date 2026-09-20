from __future__ import annotations

from datetime import date, timedelta
from typing import cast

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.exporting import CalendarExportOptions, CalendarExportScope
from officeflow.application.tasks import TaskQuery


class CalendarExportDialog(QDialog):
    def __init__(
        self,
        *,
        query: TaskQuery,
        selected_task_ids: tuple[int, ...] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._query = query
        self._selected_task_ids = selected_task_ids
        self.setWindowTitle("캘린더 내보내기")
        self.setModal(True)
        self.resize(500, 390)
        self.setMinimumWidth(440)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)
        title = QLabel("ICS 캘린더 내보내기")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        description = QLabel(
            "출장이나 외부 확인에 필요한 일정만 골라 iCalendar 파일로 만듭니다."
        )
        description.setObjectName("mutedText")
        description.setWordWrap(True)
        root.addWidget(description)

        scope_box = QGroupBox("내보낼 범위")
        scope_layout = QVBoxLayout(scope_box)
        self.current_list_radio = QRadioButton("현재 업무 목록")
        self.current_list_radio.setObjectName("calendarExportCurrentList")
        self.current_list_radio.setChecked(True)
        scope_layout.addWidget(self.current_list_radio)
        selected_label = (
            f"선택한 업무만 ({len(selected_task_ids)}개)"
            if selected_task_ids
            else "선택한 업무만 (선택 없음)"
        )
        self.selected_tasks_radio = QRadioButton(selected_label)
        self.selected_tasks_radio.setObjectName("calendarExportSelectedTasks")
        self.selected_tasks_radio.setEnabled(bool(selected_task_ids))
        scope_layout.addWidget(self.selected_tasks_radio)
        self.date_range_radio = QRadioButton("기간 지정")
        self.date_range_radio.setObjectName("calendarExportDateRange")
        scope_layout.addWidget(self.date_range_radio)
        root.addWidget(scope_box)

        self.range_widget = QWidget()
        range_form = QFormLayout(self.range_widget)
        range_form.setContentsMargins(20, 0, 0, 0)
        today = date.today()
        self.date_from_edit = QDateEdit(QDate(today.year, today.month, today.day))
        self.date_from_edit.setObjectName("calendarExportDateFrom")
        self.date_from_edit.setCalendarPopup(True)
        self.date_from_edit.setDisplayFormat("yyyy-MM-dd")
        end = today + timedelta(days=30)
        self.date_to_edit = QDateEdit(QDate(end.year, end.month, end.day))
        self.date_to_edit.setObjectName("calendarExportDateTo")
        self.date_to_edit.setCalendarPopup(True)
        self.date_to_edit.setDisplayFormat("yyyy-MM-dd")
        dates = QWidget()
        dates_layout = QHBoxLayout(dates)
        dates_layout.setContentsMargins(0, 0, 0, 0)
        dates_layout.addWidget(self.date_from_edit)
        dates_layout.addWidget(QLabel("~"))
        dates_layout.addWidget(self.date_to_edit)
        range_form.addRow("기간", dates)
        root.addWidget(self.range_widget)

        option_box = QGroupBox("포함 옵션")
        option_layout = QVBoxLayout(option_box)
        self.include_recurring_check = QCheckBox("반복 일정 포함")
        self.include_recurring_check.setObjectName("calendarExportIncludeRecurring")
        self.include_recurring_check.setChecked(False)
        self.include_recurring_check.setToolTip(
            "선택하면 반복 규칙 전체가 ICS에 포함됩니다."
        )
        option_layout.addWidget(self.include_recurring_check)
        self.include_completed_check = QCheckBox("완료 업무 포함")
        self.include_completed_check.setObjectName("calendarExportIncludeCompleted")
        self.include_completed_check.setChecked(True)
        option_layout.addWidget(self.include_completed_check)
        root.addWidget(option_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("내보내기")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        for radio in (
            self.current_list_radio,
            self.selected_tasks_radio,
            self.date_range_radio,
        ):
            radio.toggled.connect(self._sync_range_enabled)
        self._sync_range_enabled()

    def options(self) -> CalendarExportOptions:
        if self.selected_tasks_radio.isChecked():
            scope = CalendarExportScope.SELECTED_TASKS
        elif self.date_range_radio.isChecked():
            scope = CalendarExportScope.DATE_RANGE
        else:
            scope = CalendarExportScope.CURRENT_LIST
        return CalendarExportOptions(
            scope=scope,
            query=self._query if scope is CalendarExportScope.CURRENT_LIST else None,
            selected_task_ids=(
                self._selected_task_ids
                if scope is CalendarExportScope.SELECTED_TASKS
                else ()
            ),
            date_from=(
                cast(date, self.date_from_edit.date().toPython())
                if scope is CalendarExportScope.DATE_RANGE
                else None
            ),
            date_to=(
                cast(date, self.date_to_edit.date().toPython())
                if scope is CalendarExportScope.DATE_RANGE
                else None
            ),
            include_recurring=self.include_recurring_check.isChecked(),
            include_completed=self.include_completed_check.isChecked(),
        )

    def accept(self) -> None:
        try:
            self.options()
        except ValueError as error:
            QMessageBox.warning(self, "내보내기 조건을 확인하세요.", str(error))
            return
        super().accept()

    def _sync_range_enabled(self) -> None:
        self.range_widget.setEnabled(self.date_range_radio.isChecked())

