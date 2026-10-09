"""Modeless external save workflow shared by attachment, search and cleanup views."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, QStandardPaths, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.attachment_export import (
    AttachmentExportService,
    ExportBatchResult,
    ExportStatus,
    FileReference,
    OverwritePrompt,
)
from officeflow.presentation.background import OperationWorker, finish_thread
from officeflow.presentation.theme import LIGHT_STYLESHEET


@dataclass(slots=True)
class _Consent:
    prompt: OverwritePrompt
    answered: Event = field(default_factory=Event)
    accepted: bool = False


class _ExportWorker(OperationWorker):
    consentRequested = Signal(object)


class AttachmentExportController(QObject):
    savedDirectory = Signal(str)
    busyChanged = Signal(bool)

    def __init__(
        self,
        service: AttachmentExportService,
        parent: QWidget,
        *,
        initial_directory: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._parent = parent
        self.directory = initial_directory
        self._thread: QThread | None = None
        self._worker: _ExportWorker | None = None
        self._progress: QProgressDialog | None = None
        self._dialogs: list[QDialog] = []
        self._closed = False
        self._target: Path | None = None
        self._single = False

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def _directory(self) -> Path:
        candidate = (
            Path(self.directory)
            if self.directory
            else Path(
                QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
            )
        )
        return (
            candidate
            if candidate.is_dir()
            else Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.HomeLocation))
        )

    def choose(self, references: tuple[FileReference, ...]) -> None:
        if self.busy or self._closed or not references:
            return
        single = len(references) == 1
        if single:
            try:
                reference = references[0]
                name = self._service.name(reference)
            except Exception as error:
                self._error(str(error))
                return
            chosen, _filter = QFileDialog.getSaveFileName(
                self._parent,
                "다른 이름으로 저장",
                str(self._directory() / name),
                "모든 파일 (*.*)",
                options=QFileDialog.Option.DontConfirmOverwrite,
            )
        else:
            chosen = QFileDialog.getExistingDirectory(
                self._parent, f"선택한 {len(references)}개 파일 저장 폴더", str(self._directory())
            )
        if chosen:
            if (
                single
                and Path(name).suffix
                and Path(chosen).suffix.casefold() != Path(name).suffix.casefold()
            ):
                corrected = str(Path(chosen).with_name(Path(chosen).name + Path(name).suffix))
                box = QMessageBox(self._parent)
                box.setWindowTitle("파일 형식은 변환되지 않습니다")
                box.setText(f"원래 확장자를 유지해 저장합니다.\n{corrected}")
                box.setStandardButtons(
                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
                )
                box.setDefaultButton(QMessageBox.StandardButton.Cancel)
                box.finished.connect(
                    lambda result: (
                        self.start(references, Path(corrected), single=True)
                        if result == int(QMessageBox.StandardButton.Ok)
                        else None
                    )
                )
                self._keep_dialog(box)
                return
            self.start(references, Path(chosen), single=single)

    def start(self, references: tuple[FileReference, ...], target: Path, *, single: bool) -> None:
        if self.busy or self._closed:
            return
        self._target, self._single = target, single
        thread = QThread(self)

        def operation(canceled: Callable[[], bool]) -> ExportBatchResult:
            def confirm(prompt: OverwritePrompt) -> bool:
                response = _Consent(prompt)
                worker.consentRequested.emit(response)
                while not response.answered.wait(0.1):
                    if canceled():
                        return False
                return response.accepted

            return self._service.export(
                references,
                target,
                single=single,
                cancel_requested=canceled,
                progress=lambda done, total: worker.progress.emit(done, total),
                confirm_overwrite=confirm,
            )

        worker = _ExportWorker(operation)
        worker.moveToThread(thread)
        progress = QProgressDialog(
            "파일 저장 준비 · 다른 업무와 알림은 계속 사용할 수 있습니다.",
            "취소",
            0,
            100,
            self._parent,
        )
        progress.setObjectName("attachmentExportProgress")
        progress.setWindowTitle("첨부 사본 저장")
        progress.setStyleSheet(LIGHT_STYLESHEET)
        progress.setWindowModality(Qt.WindowModality.NonModal)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        # Event.set is thread-safe. Do not queue cancellation behind the
        # worker's long-running copy slot.
        progress.canceled.connect(lambda: worker.cancel())
        worker.progress.connect(self._update_progress)
        worker.consentRequested.connect(self._confirm)
        worker.succeeded.connect(self._succeeded)
        worker.failed.connect(lambda error: self._error(str(error)))
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._finished)
        thread.finished.connect(thread.deleteLater)
        thread.started.connect(worker.run)
        self._thread, self._worker, self._progress = thread, worker, progress
        self.busyChanged.emit(True)
        progress.show()
        thread.start()

    def _keep_dialog(self, dialog: QDialog) -> None:
        dialog.setStyleSheet(LIGHT_STYLESHEET)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setModal(False)
        self._dialogs.append(dialog)

        def finished(_result: int) -> None:
            if dialog in self._dialogs:
                self._dialogs.remove(dialog)
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.show()

    def _confirm(self, value: object) -> None:
        if not isinstance(value, _Consent):
            return
        if self._closed:
            value.answered.set()
            return
        prompt = value.prompt
        box = QMessageBox(self._parent)
        box.setObjectName("attachmentExportOverwrite")
        box.setWindowTitle("기존 파일 교체 확인")
        box.setText(
            ("확인 중 대상이 변경되었습니다. 다시 확인하세요.\n" if prompt.changed else "")
            + f"기존 파일을 준비된 사본으로 교체할까요?\n{prompt.path}\n내부 첨부파일은 그대로 유지됩니다."
        )
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)

        def answered(result: int) -> None:
            value.accepted = result == int(QMessageBox.StandardButton.Yes)
            value.answered.set()

        box.finished.connect(answered)
        self._keep_dialog(box)

    def _update_progress(self, done: object, total: object) -> None:
        if self._progress and isinstance(done, int) and isinstance(total, int):
            self._progress.setValue(min(99, int(done * 100 / max(1, total))))
            self._progress.setLabelText(
                f"사본 복사/검증 중 · {done:,} / {total:,} B\n알림과 다른 업무는 계속 사용할 수 있습니다."
            )

    def _succeeded(self, value: object) -> None:
        if not isinstance(value, ExportBatchResult) or self._closed:
            return
        if any(item.status is ExportStatus.SAVED for item in value.items):
            self.directory = str(value.folder)
            self.savedDirectory.emit(self.directory)
        self.show_result(value)

    def show_result(self, result: ExportBatchResult) -> None:
        dialog = QDialog(self._parent)
        dialog.setObjectName("attachmentExportResult")
        dialog.setWindowTitle("첨부 사본 저장 결과")
        dialog.resize(560, 380)
        dialog.setMinimumSize(380, 260)
        layout = QVBoxLayout(dialog)
        counts = {
            status: sum(item.status is status for item in result.items) for status in ExportStatus
        }
        label = QLabel(
            f"{counts[ExportStatus.SAVED]}개 저장 · {counts[ExportStatus.FAILED]}개 실패 · {counts[ExportStatus.UNPROCESSED]}개 미처리"
            + (
                f" · {counts[ExportStatus.NEEDS_REVIEW]}개 확인 필요"
                if counts[ExportStatus.NEEDS_REVIEW]
                else ""
            )
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        entries = QListWidget()
        entries.setObjectName("attachmentExportResults")
        entries.setWordWrap(True)
        entries.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        states = {
            ExportStatus.SAVED: "저장됨",
            ExportStatus.FAILED: "실패",
            ExportStatus.UNPROCESSED: "미처리",
            ExportStatus.NEEDS_REVIEW: "확인 필요",
        }
        for item in result.items:
            text = (
                f"{states[item.status]} · {item.name or '선택 파일'}"
                + (f" → {item.output_path.name}\n{item.output_path}" if item.output_path else "")
                + (f"\n{item.message}" if item.message else "")
            )
            entries.addItem(text)
            entries.item(entries.count() - 1).setToolTip(text)
        layout.addWidget(entries, 1)
        caption = QLabel(
            "내부 첨부는 삭제되지 않습니다. 재시도하면 그때 저장된 최신 내용을 복사합니다."
        )
        caption.setWordWrap(True)
        layout.addWidget(caption)
        actions = QHBoxLayout()
        folder = QPushButton("폴더 열기")
        folder.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.folder)))
        )
        actions.addWidget(folder)
        retry = QPushButton("미처리 재시도")
        retry.setToolTip(
            "실패/미처리 파일만 다시 시도합니다. 확인 필요 항목은 데이터관리에서 먼저 확인하세요."
        )
        retry.setEnabled(bool(result.retry_references))
        target, single = self._target, self._single

        def retry_failed() -> None:
            if target is not None and not self.busy:
                dialog.accept()
                self.start(result.retry_references, target, single=single)

        retry.clicked.connect(retry_failed)
        actions.addWidget(retry)
        close = QPushButton("닫기")
        close.clicked.connect(dialog.accept)
        actions.addWidget(close)
        layout.addLayout(actions)
        self._keep_dialog(dialog)

    def _error(self, text: str) -> None:
        if self._closed:
            return
        box = QMessageBox(self._parent)
        box.setWindowTitle("파일 저장 확인")
        box.setText(text)
        self._keep_dialog(box)

    def _finished(self) -> None:
        if self._progress:
            self._progress.blockSignals(True)
            self._progress.hide()
            self._progress.deleteLater()
        self._thread = self._worker = self._progress = None
        self.busyChanged.emit(False)

    def shutdown(self) -> None:
        self._closed = True
        if self._worker:
            self._worker.cancel()
        for dialog in tuple(self._dialogs):
            dialog.reject()
        if self._thread:
            finish_thread(
                self._thread, parent=self._parent, label="사본 저장을 안전하게 마무리하고 있습니다…"
            )
