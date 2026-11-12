"""Pre-stage on selection; native drag only reads an already verified snapshot."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from PySide6.QtCore import QMimeData, QObject, Qt, QThread, QTimer, QUrl, Slot
from PySide6.QtGui import QCursor, QDrag
from PySide6.QtWidgets import QLabel, QListWidget, QToolTip, QWidget
from shiboken6 import isValid

from officeflow.application.attachment_export import AttachmentExportService, FileReference
from officeflow.infrastructure.attachments.drag_copies import DragCopyBatch, DragCopyStore
from officeflow.presentation.background import OperationWorker, ShutdownOwner, ShutdownScope


class AttachmentDragListWidget(QListWidget):
    """Copy-only outgoing drag; no internal item movement or drop imports."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.drag_controller: AttachmentDragController | None = None
        self.setDragEnabled(True)
        self.setAcceptDrops(False)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    def startDrag(self, supportedActions: Qt.DropAction) -> None:
        if self.drag_controller:
            self.drag_controller.start_drag()


class AttachmentDragController(ShutdownOwner, QObject):
    def __init__(
        self,
        service: AttachmentExportService,
        parent: QWidget,
        listing: QListWidget,
        status: QLabel,
        selection: Callable[[], tuple[object, ...]],
        references: Callable[[], tuple[FileReference, ...]],
        available: Callable[[], bool],
        *,
        store: DragCopyStore | None = None,
    ) -> None:
        super().__init__(parent)
        self._shutdown_scope = ShutdownScope(self, self._shutdown_completed)
        self.store = store or DragCopyStore(service)
        self.listing, self.status = listing, status
        self.selection, self.references, self.available = selection, references, available
        self._token: tuple[object, ...] = ()
        self._ready: DragCopyBatch | None = None
        self._thread: QThread | None = None
        self._worker: OperationWorker | None = None
        self._closed = False
        self._pending = False
        self._dragging = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._start)

    def select(self) -> None:
        if self._closed:
            return
        token = self.selection() if self.available() else ()
        if token == self._token:
            return
        self._token = token
        self._ready = None
        self._pending = bool(token)
        self._timer.stop()
        if self._worker:
            self._worker.cancel()
        if token and not self._closed:
            self.status.setText("끌어 복사 준비 중… 준비되면 탐색기·바탕화면으로 끄세요.")
            self._timer.start()
        else:
            self.status.hide()

    def invalidate(self) -> None:
        self._ready = None
        self._timer.stop()
        if self._worker:
            self._worker.cancel()
        self._token = ()
        self.select()

    def _start(self) -> None:
        if self._closed or self._thread is not None or not self._token or self._dragging:
            return
        self._pending = False
        self._ready = None
        token = self._token
        refs = self.references()
        thread = QThread(self)
        worker = OperationWorker(lambda canceled: (token, self.store.prepare(refs, canceled)))
        worker.moveToThread(thread)
        worker.succeeded.connect(self._prepared)
        worker.failed.connect(self._failed)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.finished.connect(worker.deleteLater)
        self._shutdown_scope.track(thread, worker, self._finished, worker.cancel)
        thread.started.connect(worker.run)
        self._thread, self._worker = thread, worker
        thread.start()

    @Slot(object)
    def _prepared(self, value: object) -> None:
        token, batch = cast(tuple[tuple[object, ...], DragCopyBatch], value)
        if not self._closed and token == self._token and self.available():
            self._ready = batch
            self.status.setText(
                f"준비 시점 사본 · {len(batch.paths)}개 · 탐색기·바탕화면으로 끌어 복사"
            )

    @Slot(object)
    def _failed(self, error: object) -> None:
        if not self._closed and self._token and not self._pending:
            self.status.setText(
                f"끌어 복사 준비 실패 · {error} · 다시 선택하거나 저장 기능을 사용하세요."
            )

    def _finished(self) -> None:
        self._thread = None
        self._worker = None
        if self._pending and not self._closed:
            self._timer.start(0)

    def start_drag(self) -> None:
        if self._closed or self._dragging or not self.available():
            return
        if self.selection() != self._token:
            self.select()
        batch = self._ready
        if batch is None:
            self.status.setText(
                "사본 준비가 끝나면 다시 끌어 주세요. 실패한 경우 파일을 다시 선택하세요."
            )
            QToolTip.showText(QCursor.pos(), self.status.text(), self.listing, msecShowTime=4000)
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path)) for path in batch.paths])
        drag = QDrag(self.listing)
        drag.setMimeData(mime)
        # This lease is retained even on cancel: a receiver may defer reading.
        try:
            self.store.offer(batch)
        except Exception as error:
            self.status.setText(f"끌어 복사를 시작하지 못했습니다 · {error}")
            QToolTip.showText(QCursor.pos(), self.status.text(), self.listing, msecShowTime=4000)
            drag.deleteLater()
            return
        self._dragging = True
        self.listing.setProperty("outgoingAttachmentDrag", True)
        try:
            drag.exec(Qt.DropAction.CopyAction, Qt.DropAction.CopyAction)
        finally:
            self._dragging = False
            if isValid(self.listing):
                self.listing.setProperty("outgoingAttachmentDrag", False)
            if isValid(drag):
                drag.deleteLater()
            if self._pending and not self._closed:
                self._timer.start(0)

    def request_shutdown(self) -> None:
        self._closed = True
        self._timer.stop()
        self._ready = None
        if self._dragging:
            QDrag.cancel()
        if self._worker:
            self._worker.cancel()
        self._shutdown_scope.request_stop()

    def _shutdown_completed(self) -> None:
        self.store.close()
