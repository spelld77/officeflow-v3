from datetime import date
from typing import cast

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.task_archive import ArchiveDateBasis, ArchiveOptions, ArchiveScope
from officeflow.presentation.compact_labels import WrappingLabel
from officeflow.presentation.dates import date_with_weekday
from officeflow.presentation.detail_widgets import CollapsibleText
from officeflow.presentation.theme import LIGHT_STYLESHEET


class TaskArchiveDialog(QDialog):
    def __init__(
        self,
        selected_task_ids: tuple[int, ...] = (),
        *,
        timezone: str = "Asia/Seoul",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setStyleSheet(LIGHT_STYLESHEET)
        self.setWindowTitle("업무 묶음 보관")
        self.resize(520, 570)
        self._ids, self._timezone = selected_task_ids, timezone
        root = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setObjectName("taskArchiveScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.viewport().setStyleSheet("background:#F5F7FA;color:#172033;")
        content = QWidget()
        content.setStyleSheet("background:#F5F7FA;color:#172033;")
        layout = QVBoxLayout(content)
        caption = WrappingLabel()
        caption.setText(
            "HTML·Excel 목록과 파일 사본을 외부 폴더에 남깁니다.\n내부 자료는 유지됩니다. 전체 백업·복원 파일은 아닙니다."
        )
        layout.addWidget(caption)
        form = QFormLayout()
        self._form = form
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.scope = QComboBox()
        self.scope.addItem("연도별", "year")
        self.scope.addItem("기간 지정", "period")
        if selected_task_ids:
            self.scope.addItem(f"선택 업무 {len(selected_task_ids)}개", "selected")
        form.addRow("범위", self.scope)
        self.year = QSpinBox()
        self.year.setRange(1900, 9998)
        self.year.setValue(date.today().year)
        form.addRow("연도", self.year)
        self.start = date_with_weekday(QDateEdit(QDate(date.today().year, 1, 1)))
        self.end = date_with_weekday(QDateEdit(QDate(date.today().year, 12, 31)))
        for edit in (self.start, self.end):
            edit.setCalendarPopup(True)
        form.addRow("시작일", self.start)
        form.addRow("종료일", self.end)
        self.basis = QComboBox()
        for label, basis in (
            ("등록일", ArchiveDateBasis.CREATED),
            ("완료일", ArchiveDateBasis.COMPLETED),
            ("최근 수정일", ArchiveDateBasis.UPDATED),
        ):
            self.basis.addItem(label, basis.value)
        form.addRow("날짜 기준", self.basis)
        layout.addLayout(form)
        self.completed = QCheckBox("완료 업무만")
        self.completed.setChecked(True)
        self.attachments = QCheckBox("현재 첨부파일 포함")
        self.attachments.setChecked(True)
        self.versions = QCheckBox("파일 보관본 포함")
        self.snapshots = QCheckBox("중요 시점의 파일 포함")
        for check in (self.completed, self.attachments, self.versions, self.snapshots):
            layout.addWidget(check)
        self.scope_summary = WrappingLabel()
        layout.addWidget(self.scope_summary)
        self.scope_details = CollapsibleText(
            "포함·제외 상세",
            "포함: 업무 정보·결과·체크리스트·업무일지·저장된 반복 회차·폴더 연결 정보와 선택한 종류의 파일. 현재 첨부에는 일반·원본·작업본이 포함됩니다.\n"
            "제외: 휴지통·해제 첨부·보기 사본·자동 변경 이력·중요 시점 정보 전체·외부 폴더 내용. 폴더 경로만 남기며 실제 폴더 파일은 수집하지 않습니다.\n"
            "반복은 저장된 회차만 포함하고 미래 회차를 생성하지 않습니다. 중요 시점의 파일을 선택해도 업무 이력 전체는 포함되지 않습니다.\n"
            "기간 양끝 날짜를 포함하며 OfficeFlow 설정 시간대를 사용합니다. 내부 자료를 삭제하거나 복원용 백업을 만드는 기능은 아닙니다.",
        )
        layout.addWidget(self.scope_details)
        layout.addStretch()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("보관 폴더 선택…")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self._accept_options)
        buttons.rejected.connect(self.reject)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        root.addWidget(buttons)
        self.scope.currentIndexChanged.connect(self._scope_changed)
        self.year.valueChanged.connect(self._update_summary)
        self.start.dateChanged.connect(self._update_summary)
        self.end.dateChanged.connect(self._update_summary)
        self.basis.currentIndexChanged.connect(self._update_summary)
        self.completed.toggled.connect(self._update_summary)
        self._scope_changed()

    def _scope_changed(self) -> None:
        scope = self.scope.currentData()
        self.year.setEnabled(scope == "year")
        self.start.setEnabled(scope == "period")
        self.end.setEnabled(scope == "period")
        self.basis.setEnabled(scope != "selected")
        self.completed.setEnabled(scope != "selected")
        for widget, visible in (
            (self.year, scope == "year"),
            (self.start, scope == "period"),
            (self.end, scope == "period"),
            (self.basis, scope != "selected"),
        ):
            self._form.setRowVisible(widget, visible)
        self.completed.setVisible(scope != "selected")
        self._update_summary()

    def _update_summary(self) -> None:
        if self.scope.currentData() == "selected":
            summary = f"선택한 업무 {len(self._ids)}개 · 날짜/완료 조건 미적용"
        else:
            period = (
                f"{self.year.value()}년"
                if self.scope.currentData() == "year"
                else f"{self.start.date().toString('yyyy-MM-dd')} ~ {self.end.date().toString('yyyy-MM-dd')}"
            )
            summary = f"대상: {period} · {self.basis.currentText()} · {'완료 업무만' if self.completed.isChecked() else '휴지통 제외 전체 상태'}\n주 화면 검색·필터와 별도"
        self.scope_summary.setText(summary)

    def options(self) -> ArchiveOptions:
        yearly = self.scope.currentData() in {"year", "selected"}
        start = (
            date(self.year.value(), 1, 1) if yearly else cast(date, self.start.date().toPython())
        )
        end = date(self.year.value(), 12, 31) if yearly else cast(date, self.end.date().toPython())
        return ArchiveOptions(
            start,
            end,
            timezone=self._timezone,
            scope=ArchiveScope.SELECTED
            if self.scope.currentData() == "selected"
            else ArchiveScope.PERIOD,
            basis=ArchiveDateBasis(self.basis.currentData()),
            completed_only=self.completed.isChecked(),
            task_ids=self._ids,
            attachments=self.attachments.isChecked(),
            versions=self.versions.isChecked(),
            snapshot_files=self.snapshots.isChecked(),
        )

    def _accept_options(self) -> None:
        try:
            self.options()
        except ValueError as error:
            QMessageBox.warning(self, "보관 범위 확인", str(error))
            return
        self.accept()
