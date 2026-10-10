from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from officeflow.application.attachment_export import ExportBatchResult, ExportStatus
from officeflow.infrastructure.backup import BackupCanceledError, BackupManager
from officeflow.infrastructure.backup_files import BackupFileHit, BackupFilePage, BackupFileSession
from officeflow.presentation.background import OperationWorker, finish_thread
from officeflow.presentation.lookup_style import SCROLLBAR_STYLE
from officeflow.presentation.theme import LIGHT_STYLESHEET

KINDS = {
    "attachment": "첨부",
    "version": "보관본",
    "snapshot_file": "중요 보관",
    "recovery": "복구 자료",
}


class BackupFilesDialog(QDialog):
    savedDirectory = Signal(str)

    def __init__(
        self, manager: BackupManager, parent: QWidget | None = None, *, directory: str | None = None
    ) -> None:
        super().__init__(parent)
        self.manager = manager
        self.directory = directory
        self.session: BackupFileSession | None = None
        self._thread: QThread | None = None
        self._worker: OperationWorker | None = None
        self._closing = False
        self._after = 0
        self._next_after = 0
        self._page_cursors = [0]
        self._has_more = False
        self.setWindowTitle("백업에서 파일 꺼내기")
        self.setStyleSheet(LIGHT_STYLESHEET)
        self.setModal(False)
        self.resize(660, 530)
        self.setMinimumSize(500, 420)
        root = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.choose_button = QPushButton("백업 선택…")
        self.choose_button.clicked.connect(self.choose)
        controls.addWidget(self.choose_button)
        self.filename = QLabel("백업 파일을 선택하세요.")
        self.filename.setTextFormat(Qt.TextFormat.PlainText)
        self.filename.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        controls.addWidget(self.filename, 1)
        root.addLayout(controls)
        hint = QLabel("선택한 파일만 외부 폴더에 저장합니다. 현재 업무·DB는 바뀌지 않습니다.")
        hint.setWordWrap(True)
        root.addWidget(hint)
        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("이름·보관명·메모·업무 검색")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self.refresh)
        self.search_button = QPushButton("검색")
        self.search_button.clicked.connect(self.refresh)
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.search_button)
        root.addLayout(search_row)
        self.list = QListWidget()
        self.list.verticalScrollBar().setStyleSheet(SCROLLBAR_STYLE)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setStyleSheet(
            "QListWidget{background:#FFFFFF;color:#172033;} QListWidget::item{padding:3px;} QListWidget::item:selected{background:#DFE8FF;color:#172033;}"
        )
        self.list.viewport().setStyleSheet("background:#FFFFFF;color:#172033;")
        self.list.itemSelectionChanged.connect(self._selection)
        root.addWidget(self.list, 1)
        self.selection_label = QLabel("Ctrl/Shift로 여러 파일 선택 · 페이지를 바꾸면 선택 해제")
        self.selection_label.setWordWrap(True)
        root.addWidget(self.selection_label)
        paging = QHBoxLayout()
        self.previous_button = QPushButton("이전 페이지")
        self.next_button = QPushButton("다음 페이지")
        self.previous_button.clicked.connect(self.previous)
        self.next_button.clicked.connect(self.next)
        self.page_label = QLabel("")
        paging.addWidget(self.previous_button)
        paging.addWidget(self.page_label)
        paging.addStretch()
        paging.addWidget(self.next_button)
        self.folder_button = QPushButton("저장 폴더 열기")
        self.folder_button.clicked.connect(self.open_folder)
        self.folder_button.hide()
        paging.addWidget(self.folder_button)
        root.addLayout(paging)
        self.status = QLabel("전체 복원이 아닌 파일 사본 저장입니다.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        actions = QHBoxLayout()
        self.save_button = QPushButton("선택 파일 저장…")
        self.save_button.clicked.connect(self.save)
        self.cancel_button = QPushButton("작업 취소")
        self.cancel_button.clicked.connect(self.cancel)
        self.close_button = QPushButton("닫기")
        self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.save_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        actions.addWidget(self.close_button)
        root.addLayout(actions)
        self._buttons(False)

    def _selection(self) -> None:
        selected = self.selected_ids()
        self.save_button.setEnabled(
            bool(selected) and self._thread is None and self.session is not None
        )
        self.selection_label.setText(
            f"선택 {len(selected)}개 · Ctrl/Shift로 여러 파일 선택 · 페이지 이동 시 선택 해제"
        )

    def selected_ids(self) -> tuple[int, ...]:
        return tuple(
            hit.id
            for item in self.list.selectedItems()
            if isinstance(hit := item.data(Qt.ItemDataRole.UserRole), BackupFileHit)
        )

    def _buttons(self, busy: bool) -> None:
        self.choose_button.setEnabled(not busy)
        available = self.session is not None and self.session.available
        self.search_button.setEnabled(not busy and available)
        self.search.setEnabled(not busy and available)
        self.previous_button.setEnabled(not busy and len(self._page_cursors) > 1)
        self.next_button.setEnabled(not busy and self._has_more)
        self.cancel_button.setEnabled(busy)
        self.list.setEnabled(not busy)
        self._selection()

    def choose(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "백업 파일 선택",
            str(self.manager.backup_directory),
            "OfficeFlow 백업 (*.ofbackup);;모든 파일 (*)",
        )
        if filename:
            self.load(Path(filename))

    def load(self, path: Path) -> None:
        if self._thread is not None:
            return
        if self.session:
            self.session.close()
        self.session = BackupFileSession(self.manager, path)
        self.filename.setText(path.name[:55])
        self.filename.setToolTip(str(path))
        self.search.clear()
        self.folder_button.hide()
        self._after, self._page_cursors = 0, [0]
        self._has_more = False
        self.list.clear()
        self.status.setText(
            "백업 전체를 검증하고 파일 목록을 준비합니다… 큰 백업은 시간이 걸립니다."
        )
        self._run(self.session.load, self._received)

    def refresh(self) -> None:
        if self.session is None or self._thread is not None:
            return
        self._after, self._page_cursors = 0, [0]
        self._request()

    def _request(self) -> None:
        if self.session is None:
            return
        session, search, after = self.session, self.search.text(), self._after
        self.status.setText("파일 목록 조회 중…")
        self._run(lambda cancel: session.page(search, after, canceled=cancel), self._received)

    def _received(self, value: object) -> None:
        if self._closing or not isinstance(value, BackupFilePage):
            return
        self.list.clear()
        for hit in value.items:
            item = QListWidgetItem(
                f"[{KINDS[hit.kind]}] {hit.name[:100]}"
                + (f" · {hit.label[:70]}" if hit.label else "")
            )
            item.setData(Qt.ItemDataRole.UserRole, hit)
            item.setToolTip(
                f"{hit.name}\n{hit.label}\n{hit.task_title}\n{hit.memo[:500]}\n{hit.member}"
            )
            self.list.addItem(item)
        self._has_more = value.has_more
        self._next_after = value.items[-1].id if value.items else self._after
        self.page_label.setText(f"{len(self._page_cursors)}페이지 · {len(value.items)}개")
        self.status.setText(
            "다음 페이지에 자료가 더 있습니다."
            if value.has_more
            else "조회 완료."
            if value.items
            else "검색 결과가 없습니다. 검색어나 백업을 확인하세요."
        )

    def previous(self) -> None:
        if len(self._page_cursors) > 1 and self._thread is None:
            self._page_cursors.pop()
            self._after = self._page_cursors[-1]
            self._request()

    def next(self) -> None:
        if self._has_more and self._thread is None:
            self._after = self._next_after
            self._page_cursors.append(self._after)
            self._request()

    def save(self) -> None:
        ids = self.selected_ids()
        if self.session is None or not ids or self._thread is not None:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "선택 파일 저장 폴더", self.directory or str(Path.home())
        )
        if folder:
            self.export(ids, Path(folder))

    def export(self, ids: tuple[int, ...], folder: Path) -> None:
        if self.session is None or self._thread is not None:
            return
        session = self.session
        self.status.setText("선택 파일 검증·저장 중…")
        self._run(lambda cancel: session.export(ids, folder, cancel, self._progress), self._saved)

    def _progress(self, done: int, total: int) -> None:
        if self._worker:
            self._worker.progress.emit(done, total)

    def _progress_received(self, done: int, total: int) -> None:
        if not self._closing:
            self.status.setText(f"선택 파일 저장 {done}/{total}")

    def _saved(self, value: object) -> None:
        if self._closing or not isinstance(value, ExportBatchResult):
            return
        self.list.clear()
        self._has_more = False
        labels = {
            ExportStatus.SAVED: "저장",
            ExportStatus.FAILED: "실패",
            ExportStatus.UNPROCESSED: "미처리",
            ExportStatus.NEEDS_REVIEW: "확인 필요",
        }
        for result in value.items:
            item = QListWidgetItem(
                f"[{labels[result.status]}] {result.output_path.name if result.output_path else result.name}"
            )
            item.setToolTip(
                str(result.output_path or "")
                + "\n"
                + result.message
                + (f"\n복구 기록: {result.journal_path}" if result.journal_path else "")
            )
            self.list.addItem(item)
        count = sum(item.status == ExportStatus.SAVED for item in value.items)
        self.status.setText(
            f"저장 {count} / 전체 {len(value.items)}개 · {str(value.folder)[-100:]}\n원본과 기존 파일은 유지했습니다. 목록은 검색 버튼으로 다시 조회합니다."
        )
        if count:
            self.directory = str(value.folder)
            self.folder_button.show()
            self.savedDirectory.emit(self.directory)

    def open_folder(self) -> None:
        if self.directory and not QDesktopServices.openUrl(QUrl.fromLocalFile(self.directory)):
            self.status.setText("저장 폴더를 열지 못했습니다. 결과 항목의 저장 위치를 확인하세요.")

    def _run(
        self, operation: Callable[[Callable[[], bool]], object], receive: Callable[[object], None]
    ) -> None:
        if self._thread is not None or self._closing:
            return
        thread = QThread(self)
        worker = OperationWorker(operation)
        self._thread, self._worker = thread, worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(receive, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        worker.progress.connect(self._progress_received, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        self._buttons(True)
        thread.start()

    def _failed(self, error: object) -> None:
        if self._closing:
            return
        self._has_more = False
        message = (
            "작업 취소. 현재 자료는 유지했습니다."
            if isinstance(error, BackupCanceledError)
            else str(error)
            if isinstance(error, (ValueError, RuntimeError))
            else "백업 파일 처리에 실패했습니다. 파일 권한·저장 공간을 확인하세요."
        )
        self.status.setText(
            message[:200] + ("… (상세는 마우스를 올려 확인)" if len(message) > 200 else "")
        )
        self.status.setToolTip(message[:2000])

    def _finished(self) -> None:
        thread = self._thread
        if thread is not None:
            thread.wait()
            thread.deleteLater()
        self._thread, self._worker = None, None
        if not self._closing:
            self._buttons(False)

    def cancel(self) -> None:
        if self._worker:
            self._worker.cancel()
            self.status.setText("취소 중… 이미 저장한 파일은 유지합니다.")

    def shutdown(self) -> None:
        self._closing = True
        self.cancel()
        thread = self._thread
        if thread is not None:
            finish_thread(thread, parent=self, label="백업 파일 작업을 마무리합니다…")
            if isValid(thread):
                thread.deleteLater()
            self._thread, self._worker = None, None
        if self.session:
            self.session.close()
            self.session = None

    def reject(self) -> None:
        self.shutdown()
        super().reject()
