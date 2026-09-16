from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.migration import (
    LegacyMigration,
    MigrationPreview,
    MigrationResult,
)
from officeflow.presentation.data_dialog import OperationWorker


class LegacyMigrationDialog(QDialog):
    quitRequested = Signal()

    def __init__(
        self,
        migration: LegacyMigration,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._migration = migration
        self._preview: MigrationPreview | None = None
        self._thread: QThread | None = None
        self._worker: OperationWorker | None = None
        self._progress: QProgressDialog | None = None

        self.setWindowTitle("OfficeFlow 2.6 데이터 가져오기")
        self.setModal(True)
        self.resize(680, 590)
        self.setMinimumSize(540, 470)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)
        title = QLabel("2.6 데이터를 3.0으로 가져오기")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        caption = QLabel(
            "원본은 수정하지 않습니다. 먼저 내용을 검사한 뒤, 현재 3.0 데이터와 합친 안전한 "
            "복원 파일을 만들고 다음 실행 때 적용합니다."
        )
        caption.setObjectName("mutedText")
        caption.setWordWrap(True)
        root.addWidget(caption)

        source_card = QFrame()
        source_card.setObjectName("contentCard")
        form = QGridLayout(source_card)
        form.setColumnStretch(1, 1)
        form.addWidget(QLabel("2.6 DB"), 0, 0)
        self.database_edit = QLineEdit()
        self.database_edit.setPlaceholderText("office_tasks.db 파일을 선택하세요")
        self.database_edit.textChanged.connect(self._source_changed)
        form.addWidget(self.database_edit, 0, 1)
        database_button = QPushButton("찾기")
        database_button.clicked.connect(self._choose_database)
        form.addWidget(database_button, 0, 2)

        form.addWidget(QLabel("첨부 폴더"), 1, 0)
        self.attachment_edit = QLineEdit()
        self.attachment_edit.setPlaceholderText("비워두면 DB 옆 saved_files를 자동으로 찾습니다")
        self.attachment_edit.textChanged.connect(self._source_changed)
        form.addWidget(self.attachment_edit, 1, 1)
        attachment_button = QPushButton("찾기")
        attachment_button.clicked.connect(self._choose_attachment_root)
        form.addWidget(attachment_button, 1, 2)
        root.addWidget(source_card)

        self.summary_label = QLabel("DB를 선택한 뒤 검사하세요.")
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)
        self.issue_view = QPlainTextEdit()
        self.issue_view.setReadOnly(True)
        self.issue_view.setPlaceholderText("검사 결과와 주의사항이 여기에 표시됩니다.")
        root.addWidget(self.issue_view, 1)

        button_row = QHBoxLayout()
        self.preview_button = QPushButton("가져오기 전 검사")
        self.preview_button.clicked.connect(self._run_preview)
        self.import_button = QPushButton("안전하게 가져오기")
        self.import_button.setObjectName("primaryButton")
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self._run_import)
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.reject)
        button_row.addWidget(self.preview_button)
        button_row.addWidget(self.import_button)
        button_row.addStretch()
        button_row.addWidget(close_button)
        root.addLayout(button_row)

        self.setTabOrder(self.database_edit, database_button)
        self.setTabOrder(database_button, self.attachment_edit)
        self.setTabOrder(self.attachment_edit, attachment_button)
        self.setTabOrder(attachment_button, self.preview_button)
        self.setTabOrder(self.preview_button, self.import_button)
        self.setTabOrder(self.import_button, close_button)

    def _choose_database(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self,
            "OfficeFlow 2.6 데이터베이스 선택",
            "",
            "SQLite 데이터베이스 (*.db *.sqlite *.sqlite3);;모든 파일 (*)",
        )
        if not name:
            return
        source = Path(name)
        self.database_edit.setText(str(source))
        default_attachments = source.parent / "saved_files"
        if not self.attachment_edit.text().strip() and default_attachments.is_dir():
            self.attachment_edit.setText(str(default_attachments))

    def _choose_attachment_root(self) -> None:
        name = QFileDialog.getExistingDirectory(self, "2.6 첨부파일 폴더 선택")
        if name:
            self.attachment_edit.setText(name)

    def _source_changed(self) -> None:
        self._preview = None
        self.import_button.setEnabled(False)

    def _run_preview(self) -> None:
        source_text = self.database_edit.text().strip()
        if not source_text:
            QMessageBox.warning(self, "DB 선택 필요", "2.6 DB 파일을 먼저 선택하세요.")
            self.database_edit.setFocus()
            return
        attachment_text = self.attachment_edit.text().strip()
        source = Path(source_text)
        attachment_root = Path(attachment_text) if attachment_text else None
        self._start(
            "v2.6 데이터와 첨부파일을 검사하는 중입니다...",
            lambda canceled: self._migration.preview(
                source,
                attachment_root,
                cancel_requested=canceled,
            ),
            self._preview_succeeded,
        )

    def _preview_succeeded(self, result: object) -> None:
        if not isinstance(result, MigrationPreview):
            return
        self._preview = result
        counts = result.importable_counts
        self.summary_label.setText(
            f"가져올 항목: 업무 {counts.tasks}개 · 업무일지 {counts.work_logs}개 · "
            f"메모 {counts.notes}개 · 첨부 {counts.attachments}개"
        )
        level_label = {"error": "오류", "warning": "주의", "info": "안내"}
        lines = [
            f"[{level_label.get(issue.level, issue.level)}] {issue.message}"
            for issue in result.issues
        ]
        if not lines:
            lines.append("검사를 통과했습니다. 발견된 문제는 없습니다.")
        self.issue_view.setPlainText("\n".join(lines))
        self.import_button.setEnabled(result.can_import)
        if not result.can_import:
            QMessageBox.warning(
                self,
                "가져오기 불가",
                "오류가 있어 가져올 수 없습니다. 검사 결과를 확인하세요.",
            )

    def _run_import(self) -> None:
        preview = self._preview
        if preview is None or not preview.can_import:
            return
        if (
            QMessageBox.question(
                self,
                "2.6 데이터 가져오기",
                "현재 3.0 데이터는 유지하면서 검사된 2.6 데이터를 합칩니다. "
                "완료 후 프로그램을 다시 실행해야 적용됩니다. 계속할까요?",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._start(
            "원본을 백업하고 3.0 형식으로 안전하게 변환하는 중입니다...",
            lambda canceled: self._migration.migrate(preview, cancel_requested=canceled),
            self._import_succeeded,
        )

    def _import_succeeded(self, result: object) -> None:
        if not isinstance(result, MigrationResult):
            return
        counts = result.imported_counts
        message = (
            f"업무 {counts.tasks}개, 업무일지 {counts.work_logs}개, 메모 {counts.notes}개, "
            f"첨부 {counts.attachments}개를 변환했습니다.\n\n"
            "현재 화면에는 아직 반영되지 않았습니다. 지금 OfficeFlow를 완전히 종료할까요?"
        )
        self.summary_label.setText(
            "가져오기 준비 완료 — OfficeFlow를 완전히 종료한 뒤 다시 실행하면 적용됩니다."
        )
        self.import_button.setEnabled(False)
        if (
            QMessageBox.question(
                self,
                "가져오기 준비 완료",
                message,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.accept()
            self.quitRequested.emit()

    def _start(
        self,
        label: str,
        operation: Callable[[Callable[[], bool]], object],
        succeeded: Callable[[object], None],
    ) -> None:
        if self._thread is not None:
            return
        thread = QThread(self)
        worker = OperationWorker(operation)
        worker.moveToThread(thread)
        progress = QProgressDialog(label, "취소", 0, 0, self)
        progress.setWindowTitle("2.6 데이터 가져오기")
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.canceled.connect(worker.cancel)
        thread.started.connect(worker.run)
        worker.succeeded.connect(succeeded)
        worker.failed.connect(self._operation_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._operation_finished)
        self._thread = thread
        self._worker = worker
        self._progress = progress
        thread.start()

    def _operation_failed(self, error: Any) -> None:
        QMessageBox.critical(self, "가져오기 실패", str(error))

    def _operation_finished(self) -> None:
        if self._progress is not None:
            self._progress.close()
        self._progress = None
        self._worker = None
        self._thread = None

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
