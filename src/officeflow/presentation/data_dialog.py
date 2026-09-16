from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.exporting import ExportService
from officeflow.application.migration import LegacyMigration
from officeflow.application.tasks import TaskQuery
from officeflow.infrastructure.backup import BackupInfo, BackupManager, BackupManifest


class OperationWorker(QObject):
    succeeded = Signal(object)
    failed = Signal(object)
    finished = Signal()

    def __init__(self, operation: Callable[[Callable[[], bool]], object]) -> None:
        super().__init__()
        self._operation = operation
        self._canceled = Event()

    def cancel(self) -> None:
        self._canceled.set()

    @Slot()
    def run(self) -> None:
        try:
            result = self._operation(self._canceled.is_set)
        except Exception as error:
            self.failed.emit(error)
        else:
            self.succeeded.emit(result)
        finally:
            self.finished.emit()


class DataManagementDialog(QDialog):
    quitRequested = Signal()

    def __init__(
        self,
        *,
        export_service: ExportService,
        backup_manager: BackupManager,
        query: TaskQuery,
        migration_service: LegacyMigration | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._export_service = export_service
        self._backup_manager = backup_manager
        self._query = query
        self._migration_service = migration_service
        self._thread: QThread | None = None
        self._worker: OperationWorker | None = None
        self._progress: QProgressDialog | None = None

        self.setWindowTitle("내보내기 및 백업")
        self.setModal(True)
        self.resize(560, 540)
        self.setMinimumSize(480, 470)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)
        title = QLabel("데이터 내보내기 및 보호")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        caption = QLabel(
            "현재 업무 목록을 파일로 내보내거나, 전체 데이터를 검증 가능한 백업으로 보관합니다."
        )
        caption.setObjectName("mutedText")
        caption.setWordWrap(True)
        root.addWidget(caption)
        root.addWidget(
            self._section(
                "내보내기",
                "Excel은 현재 화면의 검색과 필터를 적용합니다. ICS는 모든 원본 일정과 반복 규칙을 포함합니다.",
                (("현재 목록 Excel", self._export_excel), ("전체 일정 ICS", self._export_ics)),
            )
        )
        if self._migration_service is not None:
            root.addWidget(
                self._section(
                    "이전 버전 가져오기",
                    "OfficeFlow 2.6 DB를 먼저 검사한 뒤 현재 3.0 데이터에 안전하게 합칩니다.",
                    (("2.6 데이터 가져오기", self._open_legacy_migration),),
                )
            )
        root.addWidget(
            self._section(
                "백업 및 복원",
                "DB·첨부파일·설정을 함께 검증합니다. 복원은 다음 실행 때 적용되며 현재 데이터는 먼저 자동 보관됩니다.",
                (("지금 백업", self._create_backup), ("백업에서 복원", self._stage_restore)),
            )
        )
        self.status_label = QLabel("준비됨")
        self.status_label.setObjectName("mutedText")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        root.addStretch()
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.accept)
        root.addWidget(close_button)

    def _open_legacy_migration(self) -> None:
        if self._migration_service is None:
            return
        from officeflow.presentation.migration_dialog import LegacyMigrationDialog

        dialog = LegacyMigrationDialog(self._migration_service, self)
        dialog.quitRequested.connect(self.quitRequested.emit)
        dialog.exec()

    def _section(
        self,
        title: str,
        description: str,
        actions: tuple[tuple[str, Callable[[], None]], ...],
    ) -> QWidget:
        card = QFrame()
        card.setObjectName("contentCard")
        layout = QVBoxLayout(card)
        heading = QLabel(title)
        heading.setStyleSheet("font-weight: 700;")
        layout.addWidget(heading)
        detail = QLabel(description)
        detail.setObjectName("mutedText")
        detail.setWordWrap(True)
        layout.addWidget(detail)
        row = QHBoxLayout()
        for label, callback in actions:
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        return card

    def _export_excel(self) -> None:
        name, _ = QFileDialog.getSaveFileName(
            self, "현재 업무 목록 내보내기", "OfficeFlow-업무.xlsx", "Excel (*.xlsx)"
        )
        if not name:
            return
        destination = Path(name).with_suffix(".xlsx")
        self._start(
            "Excel 파일을 만들고 검증하는 중입니다...",
            lambda canceled: self._export_service.export_excel(
                self._query, destination, cancel_requested=canceled
            ),
            lambda result: f"Excel 내보내기 완료: {result}",
        )

    def _export_ics(self) -> None:
        name, _ = QFileDialog.getSaveFileName(
            self, "전체 일정 내보내기", "OfficeFlow-일정.ics", "iCalendar (*.ics)"
        )
        if not name:
            return
        destination = Path(name).with_suffix(".ics")
        self._start(
            "일정 파일을 만드는 중입니다...",
            lambda canceled: self._export_service.export_calendar(
                destination, cancel_requested=canceled
            ),
            lambda result: f"ICS 내보내기 완료: {result}",
        )

    def _create_backup(self) -> None:
        self._start(
            "데이터베이스와 첨부파일을 백업하고 검증하는 중입니다...",
            lambda canceled: self._backup_manager.create_backup(
                reason="manual", cancel_requested=canceled
            ),
            self._backup_message,
        )

    def _stage_restore(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self, "OfficeFlow 백업 선택", "", "OfficeFlow 백업 (*.ofbackup)"
        )
        if not name:
            return
        if (
            QMessageBox.question(
                self,
                "복원 예약",
                "선택한 백업을 검증한 뒤 다음 실행 때 적용합니다. 계속할까요?",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._start(
            "백업을 검증하고 복원을 예약하는 중입니다...",
            lambda canceled: self._backup_manager.stage_restore(
                Path(name), cancel_requested=canceled
            ),
            self._restore_message,
        )

    def _start(
        self,
        label: str,
        operation: Callable[[Callable[[], bool]], object],
        success_message: Callable[[Any], str],
    ) -> None:
        if self._thread is not None:
            return
        thread = QThread(self)
        worker = OperationWorker(operation)
        worker.moveToThread(thread)
        progress = QProgressDialog(label, "취소", 0, 0, self)
        progress.setWindowTitle("데이터 작업")
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.canceled.connect(worker.cancel)
        thread.started.connect(worker.run)
        worker.succeeded.connect(lambda result: self._operation_succeeded(result, success_message))
        worker.failed.connect(self._operation_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._operation_finished)
        self._thread = thread
        self._worker = worker
        self._progress = progress
        thread.start()

    def _operation_succeeded(self, result: object, message: Callable[[Any], str]) -> None:
        self.status_label.setText(message(result))

    def _operation_failed(self, error: object) -> None:
        self.status_label.setText(f"작업 실패: {error}")
        QMessageBox.critical(self, "데이터 작업 실패", str(error))

    def _operation_finished(self) -> None:
        if self._progress is not None:
            self._progress.close()
        self._progress = None
        self._worker = None
        self._thread = None

    @staticmethod
    def _backup_message(result: object) -> str:
        if not isinstance(result, BackupInfo):
            return "백업이 완료되었습니다."
        return f"백업 완료: {result.path} (첨부파일 {result.attachment_count}개)"

    def _restore_message(self, result: object) -> str:
        if not isinstance(result, BackupManifest):
            return "복원이 예약되었습니다. OfficeFlow를 완전히 종료한 뒤 다시 실행하세요."
        message = (
            "백업 검증을 통과했습니다. OfficeFlow를 완전히 종료한 뒤 다시 실행하면 "
            f"DB {sum(result.table_counts.values())}건과 첨부 {len(result.attachments)}개를 복원합니다."
        )
        QMessageBox.information(self, "복원 예약 완료", message)
        return message

    def reject(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            return
        super().reject()

    def accept(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            return
        super().accept()
