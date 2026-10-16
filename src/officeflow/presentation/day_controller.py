from __future__ import annotations

from datetime import date

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from shiboken6 import isValid

from officeflow.application.day_overview import DayCursor, DayOverviewResult, DayOverviewService
from officeflow.presentation.background import OperationWorker, finish_thread


class DayOverviewController(QObject):
    ready = Signal(object)
    failed = Signal(object)

    def __init__(self, service: DayOverviewService, parent: QObject) -> None:
        super().__init__(parent)
        self.service = service
        self._generation = 0
        self._active = 0
        self._pending: tuple[date, str, str, DayCursor | None] | None = None
        self._thread: QThread | None = None
        self._worker: OperationWorker | None = None
        self._closed = False

    def request(
        self, day: date, timezone: str, search: str, cursor: DayCursor | None = None
    ) -> None:
        if self._closed:
            return
        self._generation += 1
        self._pending = day, timezone, search, cursor
        if self._worker:
            self._worker.cancel()
        else:
            self._start()

    def cancel(self) -> None:
        self._generation += 1
        self._pending = None
        if self._worker:
            self._worker.cancel()

    def _start(self) -> None:
        if self._closed or self._pending is None:
            return
        day, timezone, search, cursor = self._pending
        self._pending = None
        self._active = self._generation
        thread = QThread(self)
        worker = OperationWorker(
            lambda canceled: self.service.query(
                day, timezone=timezone, search=search, cursor=cursor, cancel_requested=canceled
            )
        )
        self._thread, self._worker = thread, worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        thread.start()

    @Slot(object)
    def _ready(self, result: DayOverviewResult) -> None:
        if not self._closed and self._active == self._generation:
            self.ready.emit(result)

    @Slot(object)
    def _failed(self, error: object) -> None:
        if not self._closed and self._active == self._generation:
            self.failed.emit(error)

    @Slot()
    def _finished(self) -> None:
        if self._thread:
            self._thread.wait()
            self._thread.deleteLater()
        self._thread = None
        self._worker = None
        self._start()

    def shutdown(self) -> None:
        self._closed = True
        self.cancel()
        thread = self._thread
        if thread is not None:
            finish_thread(thread)
            if isValid(thread):
                thread.deleteLater()
        self._thread = None
        self._worker = None
