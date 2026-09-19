from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.exporting import ExportService
from officeflow.application.migration import LegacyMigration
from officeflow.application.tasks import TaskQuery
from officeflow.infrastructure.backup import (
    BackupInfo,
    BackupManager,
    BackupManifest,
    DataUsageSnapshot,
)


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
        self._usage_loading = False
        self._refresh_usage_after_finish = False

        self.setWindowTitle("데이터 관리")
        self.setModal(True)
        self.resize(650, 600)
        self.setMinimumSize(500, 480)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)
        title = QLabel("데이터 관리")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        caption = QLabel(
            "저장 공간과 누적 현황을 확인하고 데이터를 내보내거나 백업할 수 있습니다."
        )
        caption.setObjectName("mutedText")
        caption.setWordWrap(True)
        root.addWidget(caption)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("dataManagementTabs")
        self.tabs.addTab(self._build_usage_tab(), "사용량")
        self._tools_tab_index = self.tabs.addTab(self._build_tools_tab(), "내보내기 · 백업")
        root.addWidget(self.tabs, 1)

        self.status_label = QLabel("저장 공간 확인을 준비하고 있습니다.")
        self.status_label.setObjectName("mutedText")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.accept)
        root.addWidget(close_button)
        QTimer.singleShot(0, self._refresh_usage)

    def _build_usage_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 14, 10, 10)
        heading = QHBoxLayout()
        title = QLabel("현재 데이터 현황")
        title.setStyleSheet("font-weight: 700;")
        heading.addWidget(title)
        heading.addStretch()
        self.usage_refresh_button = QPushButton("새로 고침")
        self.usage_refresh_button.setObjectName("refreshDataUsageButton")
        self.usage_refresh_button.clicked.connect(self._refresh_usage)
        heading.addWidget(self.usage_refresh_button)
        layout.addLayout(heading)

        description = QLabel(
            "파일을 삭제하지 않고 DB, 첨부파일, 백업과 휴지통의 크기만 확인합니다."
        )
        description.setObjectName("mutedText")
        description.setWordWrap(True)
        layout.addWidget(description)

        card = QFrame()
        card.setObjectName("contentCard")
        card_layout = QVBoxLayout(card)
        card_layout.setSpacing(10)
        self.usage_total_label = QLabel("전체 사용량을 확인하는 중입니다.")
        self.usage_total_label.setObjectName("dataUsageTotal")
        self.usage_total_label.setStyleSheet("font-size: 20px; font-weight: 700;")
        card_layout.addWidget(self.usage_total_label)
        self.usage_task_label = QLabel()
        self.usage_task_label.setObjectName("dataUsageTasks")
        self.usage_task_label.setWordWrap(True)
        card_layout.addWidget(self.usage_task_label)
        self.usage_storage_label = QLabel()
        self.usage_storage_label.setObjectName("dataUsageStorage")
        self.usage_storage_label.setWordWrap(True)
        card_layout.addWidget(self.usage_storage_label)
        self.usage_issue_label = QLabel()
        self.usage_issue_label.setObjectName("dataUsageIssues")
        self.usage_issue_label.setWordWrap(True)
        card_layout.addWidget(self.usage_issue_label)
        layout.addWidget(card)

        largest_card = QFrame()
        largest_card.setObjectName("contentCard")
        largest_layout = QVBoxLayout(largest_card)
        largest_title = QLabel("용량이 큰 첨부파일")
        largest_title.setStyleSheet("font-weight: 700;")
        largest_layout.addWidget(largest_title)
        self.usage_largest_label = QLabel("확인 중입니다.")
        self.usage_largest_label.setObjectName("dataUsageLargestFiles")
        self.usage_largest_label.setWordWrap(True)
        self.usage_largest_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        largest_layout.addWidget(self.usage_largest_label)
        layout.addWidget(largest_card)
        layout.addStretch()
        return tab

    def _build_tools_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 14, 10, 10)
        layout.setSpacing(10)
        layout.addWidget(
            self._section(
                "내보내기",
                "Excel은 현재 화면의 검색과 필터를 적용합니다. ICS는 모든 원본 일정과 반복 규칙을 포함합니다.",
                (("현재 목록 Excel", self._export_excel), ("전체 일정 ICS", self._export_ics)),
            )
        )
        if self._migration_service is not None:
            layout.addWidget(
                self._section(
                    "이전 버전 가져오기",
                    "OfficeFlow 2.6 DB를 먼저 검사한 뒤 현재 3.0 데이터에 안전하게 합칩니다.",
                    (("2.6 데이터 가져오기", self._open_legacy_migration),),
                )
            )
        layout.addWidget(
            self._section(
                "백업 및 복원",
                "DB·첨부파일·설정을 함께 검증합니다. 복원은 다음 실행 때 적용되며 현재 데이터는 먼저 자동 보관됩니다.",
                (("지금 백업", self._create_backup), ("백업에서 복원", self._stage_restore)),
            )
        )
        layout.addStretch()
        return tab

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

    def _refresh_usage(self) -> None:
        if self._thread is not None:
            self.status_label.setText("다른 데이터 작업이 끝난 뒤 다시 시도하세요.")
            return
        self._usage_loading = True
        self.usage_refresh_button.setEnabled(False)
        self.tabs.setTabEnabled(self._tools_tab_index, False)
        self.status_label.setText("저장 공간과 파일 연결 상태를 확인하는 중입니다...")
        self._start(
            "데이터 사용량을 확인하는 중입니다...",
            lambda canceled: self._backup_manager.inspect_data_usage(
                cancel_requested=canceled
            ),
            self._usage_message,
            show_progress=False,
        )

    def _usage_message(self, result: object) -> str:
        if not isinstance(result, DataUsageSnapshot):
            return "데이터 사용량을 확인했습니다."
        self.usage_total_label.setText(
            f"현재 사용량 {self._format_bytes(result.total_bytes)}"
        )
        current_tasks = result.task_count - result.trash_task_count
        self.usage_task_label.setText(
            "업무  "
            f"현재 {current_tasks:,}개 · 진행/대기 {result.open_task_count:,}개 · "
            f"완료 {result.completed_task_count:,}개 · 보관 {result.archived_task_count:,}개\n"
            f"휴지통  {result.trash_task_count:,}개 · "
            f"30일 경과 {result.aged_trash_task_count:,}개"
        )
        self.usage_storage_label.setText(
            f"DB  {self._format_bytes(result.database_bytes)}\n"
            f"첨부파일  {self._format_bytes(result.stored_attachment_bytes)} "
            f"({result.stored_attachment_count:,}개 저장 / {result.linked_attachment_count:,}개 등록)\n"
            f"백업  {self._format_bytes(result.backup_bytes)} ({result.backup_count:,}개)"
        )
        issues: list[str] = []
        if result.missing_attachment_count:
            issues.append(f"파일 누락 {result.missing_attachment_count:,}개")
        if result.orphan_attachment_count:
            issues.append(
                "연결 해제 파일 "
                f"{result.orphan_attachment_count:,}개 "
                f"({self._format_bytes(result.orphan_attachment_bytes)})"
            )
        if result.aged_trash_task_count:
            issues.append(f"30일 지난 휴지통 업무 {result.aged_trash_task_count:,}개")
        self.usage_issue_label.setText(
            "확인 필요  " + " · ".join(issues)
            if issues
            else "확인 필요 항목이 없습니다."
        )
        self.usage_issue_label.setStyleSheet(
            "color: #B45309; font-weight: 600;" if issues else "color: #2E7D5B;"
        )
        self.usage_largest_label.setText(
            "\n".join(
                f"{index}. {item.name} · {self._format_bytes(item.size_bytes)}"
                + (" · 연결 해제" if item.orphaned else "")
                for index, item in enumerate(result.largest_files, start=1)
            )
            or "저장된 첨부파일이 없습니다."
        )
        return "데이터 현황을 새로 확인했습니다."

    @staticmethod
    def _format_bytes(size_bytes: int) -> str:
        size = float(max(0, size_bytes))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{int(size):,} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
            size /= 1024
        return f"{size_bytes:,} B"

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
        *,
        show_progress: bool = True,
    ) -> None:
        if self._thread is not None:
            self.status_label.setText("다른 데이터 작업이 진행 중입니다.")
            return
        thread = QThread(self)
        worker = OperationWorker(operation)
        worker.moveToThread(thread)
        progress: QProgressDialog | None = None
        if show_progress:
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
        if isinstance(result, BackupInfo):
            self._refresh_usage_after_finish = True

    def _operation_failed(self, error: object) -> None:
        self.status_label.setText(f"작업 실패: {error}")
        QMessageBox.critical(self, "데이터 작업 실패", str(error))

    def _operation_finished(self) -> None:
        if self._progress is not None:
            self._progress.close()
        self._progress = None
        self._worker = None
        self._thread = None
        if self._usage_loading:
            self._usage_loading = False
            self.usage_refresh_button.setEnabled(True)
            self.tabs.setTabEnabled(self._tools_tab_index, True)
        if self._refresh_usage_after_finish:
            self._refresh_usage_after_finish = False
            QTimer.singleShot(0, self._refresh_usage)

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
