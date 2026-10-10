from __future__ import annotations

from collections.abc import Callable
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QThread, Slot
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.data_inspection import DataInspectionService, InspectionReport
from officeflow.presentation.background import OperationWorker, finish_thread
from officeflow.presentation.theme import LIGHT_STYLESHEET

STATUS_LABELS = {
    "normal": "기준과 같음",
    "modified": "수정됨(편집 가능)",
    "frozen_changed": "보관 후 변경됨",
    "missing": "파일 누락",
    "unavailable": "확인 불가(잠금·보호·검사 중 변경)",
    "no_baseline": "비교 기준 없음",
    "reference_mismatch": "보관 원장 기준 불일치",
}


class DataInspectionPanel(QWidget):
    def __init__(
        self,
        factory: Callable[[], DataInspectionService],
        parent: QWidget | None = None,
        *,
        timezone: str = "Asia/Seoul",
    ) -> None:
        super().__init__(parent)
        self._factory = factory
        self._timezone = timezone
        self._thread: QThread | None = None
        self._worker: OperationWorker | None = None
        self._closing = False
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            LIGHT_STYLESHEET + "\nQWidget#dataInspectionPanel {background:#F5F7FA;color:#172033;}"
        )
        self.setMinimumHeight(300)
        self.setObjectName("dataInspectionPanel")
        layout = QVBoxLayout(self)
        hint = QLabel("읽기 전용 검사 · 파일을 수정하거나 삭제하지 않습니다.")
        hint.setToolTip(
            "첨부·원본·작업본·개별 보관·중요 보관 파일을 검사합니다. 휴지통과 해제 첨부도 포함합니다. 일반 파일의 수정은 손상이 아닙니다. 보기 사본·미확정 작업·외부 파일은 제외합니다."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        actions = QHBoxLayout()
        self.start_button = QPushButton("자료 검사 시작")
        self.start_button.setObjectName("startDataInspection")
        self.start_button.clicked.connect(self.start)
        self.cancel_button = QPushButton("취소")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        actions.addWidget(self.start_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.summary = QLabel(
            "아직 검사하지 않았습니다. 문서를 저장하고 닫으면 더 정확히 확인할 수 있습니다."
        )
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.results = QListWidget()
        self.results.setObjectName("inspectionResults")
        self.results.setMinimumHeight(100)
        self.results.setStyleSheet(
            "QListWidget {color:#172033;background:#FFFFFF;} QListWidget::item:selected {color:#172033;background:#DFE8FF;}"
        )
        self.results.viewport().setStyleSheet("color:#172033;background:#FFFFFF;")
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        layout.addWidget(self.results, 1)

    @Slot()
    def start(self) -> None:
        if self._thread or self._closing:
            return
        self.results.clear()
        self.summary.setText("검사 범위를 확인하고 있습니다…")
        service = self._factory()
        thread = QThread(self)
        worker = OperationWorker(lambda canceled: service.run(canceled, worker.progress.emit))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._progress, Qt.ConnectionType.QueuedConnection)
        worker.succeeded.connect(self._ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        self._thread, self._worker = thread, worker
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        thread.start()

    @Slot()
    def cancel(self) -> None:
        if self._worker:
            self._worker.cancel()
            self.cancel_button.setEnabled(False)
            self.summary.setText("검사를 취소하고 읽기 핸들을 마무리하고 있습니다…")

    @Slot(object, object)
    def _progress(self, done: int, total: int) -> None:
        if not self._closing:
            self.summary.setText(f"확인 {done:,}개 · 시작 범위 {total:,}개")

    @Slot(object)
    def _ready(self, value: object) -> None:
        if self._closing or not isinstance(value, InspectionReport):
            return
        summary = " · ".join(
            f"{STATUS_LABELS[key]} {count:,}개" for key, count in value.counts.items()
        )
        title = "취소됨(부분 결과)" if value.canceled else "검사 완료"
        title += " · " + value.completed_at.astimezone(ZoneInfo(self._timezone)).strftime(
            "%Y-%m-%d %H:%M"
        )
        self.summary.setText(f"{title}\n확인 {value.checked:,}개 / 시작 범위 {value.total:,}개")
        self.summary.setToolTip("검사 시점의 결과이며 이후 변경은 다시 검사해야 합니다.")
        self.results.addItem(summary or "대상 파일 없음")
        for issue in value.issues:
            item = QListWidgetItem(f"{STATUS_LABELS[issue.status]} · {issue.target.name}")
            item.setToolTip(
                f"{issue.target.kinds}\n{issue.target.name}\n관리 상대 경로: {issue.target.relative_path}"
            )
            self.results.addItem(item)
        if value.omitted:
            self.results.addItem(
                f"상세 표시 상한 500개 · 추가 {value.omitted:,}개는 위 요약에 포함됩니다."
            )

    @Slot(object)
    def _failed(self, error: object) -> None:
        if not self._closing:
            self.summary.setText(
                f"검사를 완료하지 못했습니다({type(error).__name__}). 자료는 변경하지 않았습니다. 잠시 후 다시 검사하세요."
            )

    @Slot()
    def _finished(self) -> None:
        if self._thread:
            self._thread.wait()
            self._thread.deleteLater()
        self._thread = None
        self._worker = None
        if not self._closing:
            self.start_button.setEnabled(True)
            self.cancel_button.setEnabled(False)

    def shutdown(self) -> None:
        self._closing = True
        self.cancel()
        thread = self._thread
        if thread:
            from shiboken6 import isValid

            thread.quit()
            finish_thread(thread, parent=self)
            if isValid(thread):
                thread.deleteLater()
        self._thread = None
        self._worker = None
