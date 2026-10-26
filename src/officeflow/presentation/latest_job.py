"""One reusable reader, one latest pending request, no stale UI deliveries."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from officeflow.presentation.background import finish_thread


class _Reader(QObject):
    finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.finalizer: Callable[[], None] | None = None

    @Slot()
    def dispose(self) -> None:
        if self.finalizer:
            self.finalizer()

    @Slot(object)
    def run(self, value: object) -> None:
        revision, operation = cast(tuple[int, Callable[[], object]], value)
        try:
            self.finished.emit((revision, operation(), None))
        except Exception as error:
            # Never retain exception tracebacks/frames across requests.
            self.finished.emit((revision, None, type(error).__name__))


class LatestJob(QObject):
    requested = Signal(object)
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._reader: _Reader | None = None
        self._revision = 0
        self._active = False
        self._pending: tuple[int, Callable[[], object]] | None = None
        self._closed = False

    def submit(self, operation: Callable[[], object]) -> None:
        if self._closed:
            return
        self._revision += 1
        value = (self._revision, operation)
        if self._active:
            self._pending = value
            return
        self._start(value)

    def _start(self, value: tuple[int, Callable[[], object]]) -> None:
        if self._thread is None:
            self._thread = QThread(self)
            self._reader = _Reader()
            self._reader.moveToThread(self._thread)
            self.requested.connect(self._reader.run, Qt.ConnectionType.QueuedConnection)
            self._reader.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
            self._thread.finished.connect(self._reader.dispose, Qt.ConnectionType.DirectConnection)
            self._thread.finished.connect(self._reader.deleteLater)
            self._thread.start()
        self._active = True
        self.requested.emit(value)

    @Slot(object)
    def _finished(self, value: object) -> None:
        revision, result, error = cast(tuple[int, object, str | None], value)
        self._active = False
        pending, self._pending = self._pending, None
        if not self._closed and revision == self._revision:
            if error:
                self.failed.emit(error)
            else:
                self.ready.emit(result)
        if pending is not None and not self._closed and not self._active:
            self._start(pending)

    def invalidate(self) -> None:
        self._revision += 1
        self._pending = None

    def shutdown(self, finalizer: Callable[[], None] | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self.invalidate()
        if self._thread is not None:
            if self._reader is not None:
                self._reader.finalizer = finalizer
            self._thread.quit()
            finish_thread(self._thread)
            self._thread.deleteLater()
        elif finalizer:
            finalizer()
        self._thread, self._reader = None, None
