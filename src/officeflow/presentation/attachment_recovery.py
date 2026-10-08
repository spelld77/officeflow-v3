"""Explicit, modeless review of exact journaled external file operations."""

from collections.abc import Callable

from PySide6.QtCore import Qt, QThread, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.attachment_export import AttachmentExportService, ExportRecoveryItem
from officeflow.presentation.background import OperationWorker, finish_thread
from officeflow.presentation.theme import LIGHT_STYLESHEET


class AttachmentRecoveryDialog(QDialog):
    def __init__(
        self,
        service: AttachmentExportService,
        items: tuple[ExportRecoveryItem, ...],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._service, self._items = service, items
        self._thread: QThread | None = None
        self._worker: OperationWorker | None = None
        self._consent: QMessageBox | None = None
        self.setObjectName("attachmentRecoveryDialog")
        self.setWindowTitle("중단된 사본 저장 확인")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setStyleSheet(LIGHT_STYLESHEET)
        self.resize(640, 440)
        layout = QVBoxLayout(self)
        notice = QLabel(
            "저장 결과 파일은 삭제하지 않습니다. 내용이 바뀌거나 소유를 확인할 수 없는 자료는 보존합니다. 한 번에 최대 200개 기록을 확인합니다."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.list = QListWidget()
        self.list.setObjectName("attachmentRecoveryList")
        for item in items:
            self.list.addItem(item.message.splitlines()[0])
            self.list.item(self.list.count() - 1).setToolTip(item.message)
        layout.addWidget(self.list, 1)
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        layout.addWidget(self.detail, 1)
        actions = QGridLayout()
        self.folder = QPushButton("위치 열기")
        self.restore = QPushButton("기존 파일 복구…")
        self.cleanup = QPushButton("준비/복구 파일 정리…")
        close = QPushButton("닫기")
        for index, button in enumerate((self.folder, self.restore, self.cleanup, close)):
            actions.addWidget(button, index // 2, index % 2)
        layout.addLayout(actions)
        self.folder.clicked.connect(self._open_folder)
        self.restore.clicked.connect(lambda: self._confirm(True))
        self.cleanup.clicked.connect(lambda: self._confirm(False))
        close.clicked.connect(self.reject)
        self.list.currentRowChanged.connect(self._selection)
        self.list.setCurrentRow(0)

    def _selection(self, row: int) -> None:
        item = self._items[row] if 0 <= row < len(self._items) else None
        self.detail.setPlainText(item.message if item else "확인할 기록이 없습니다.")
        self.folder.setEnabled(item is not None)
        self.restore.setEnabled(bool(item and item.can_restore and not self._thread))
        self.cleanup.setEnabled(bool(item and item.can_cleanup and not self._thread))

    def _open_folder(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self._items):
            item = self._items[row]
            path = item.destination.parent if item.destination else item.journal.parent
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _confirm(self, restore: bool) -> None:
        row = self.list.currentRow()
        if self._thread or self._consent or not 0 <= row < len(self._items):
            return
        item = self._items[row]
        box = QMessageBox(self)
        box.setStyleSheet(LIGHT_STYLESHEET)
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.setWindowTitle("기존 파일 복구" if restore else "준비/복구 파일 정리")
        box.setText(
            (
                "빈 원래 위치에 기존 파일을 복구합니다. 다른 파일을 덮어쓰지 않습니다."
                if restore
                else "확인된 준비/복구 파일과 작업 기록만 삭제합니다. 외부 저장 결과와 내부 첨부는 그대로 유지됩니다."
            )
            + "\n\n"
            + item.message
        )
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        self._consent = box

        def answered(result: int) -> None:
            self._consent = None
            box.deleteLater()
            if result == int(QMessageBox.StandardButton.Yes):
                self._run(
                    lambda canceled: self._service.recover(item, restore=restore, canceled=canceled)
                )

        box.finished.connect(answered)
        box.show()

    def _run(self, operation: Callable[[Callable[[], bool]], str]) -> None:
        thread = QThread(self)
        worker = OperationWorker(operation)
        worker.moveToThread(thread)
        worker.succeeded.connect(
            lambda value: self.detail.setPlainText(
                str(value) + "\n닫고 다시 확인하면 최신 상태를 볼 수 있습니다."
            )
        )
        worker.failed.connect(lambda error: self.detail.setPlainText(str(error)))
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.finished.connect(worker.deleteLater)
        thread.started.connect(worker.run)

        def finished() -> None:
            self._worker = self._thread = None
            # Keep the stale snapshot from being acted on twice.
            self.restore.setEnabled(False)
            self.cleanup.setEnabled(False)

        thread.finished.connect(finished)
        thread.finished.connect(thread.deleteLater)
        self._thread, self._worker = thread, worker
        self.restore.setEnabled(False)
        self.cleanup.setEnabled(False)
        thread.start()

    def shutdown(self) -> None:
        if self._consent:
            self._consent.reject()
        if self._worker:
            self._worker.cancel()
        if self._thread:
            finish_thread(
                self._thread, parent=self, label="파일 복구를 안전하게 마무리하고 있습니다…"
            )

    def reject(self) -> None:
        self.shutdown()
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)
