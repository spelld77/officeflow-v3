from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from time import monotonic

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QDialog, QWidget
from shiboken6 import isValid


class OperationWorker(QObject):
    succeeded = Signal(object)
    failed = Signal(object)
    # Byte counters can exceed Qt's signed 32-bit int for large attachments.
    progress = Signal(object, object)
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


@dataclass
class _ThreadEntry:
    thread: QThread
    worker: QObject
    completed: Callable[[], None] | None
    cancel: Callable[[], None] | None
    delete_thread: bool = True
    persistent: bool = False
    joined: bool = False


class ShutdownScope(QObject):
    """Own workers through native join; prepare all Qt connections before start.

    Each owner has a scope, so dialogs also work independently of MainWindow.
    Parent scopes include their QObject descendants. There is no global registry.
    """

    def __init__(self, owner: QObject, completed: Callable[[], None]) -> None:
        super().__init__(owner)
        self.owner = owner
        self._completed = completed
        self._entries: dict[QThread, _ThreadEntry] = {}
        self.stopping = False
        self.is_stopped = False
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._poll)

    def track(
        self,
        thread: QThread,
        worker: QObject,
        completed: Callable[[], None] | None = None,
        cancel: Callable[[], None] | None = None,
        *,
        delete_thread: bool = True,
        persistent: bool = False,
    ) -> None:
        if self.stopping:
            raise RuntimeError("Cannot start work after shutdown was requested")
        if thread in self._entries:
            raise RuntimeError("Thread is already registered")
        self._entries[thread] = _ThreadEntry(
            thread, worker, completed, cancel, delete_thread, persistent
        )
        if not persistent:
            self._timer.start()

    def _children(self) -> list[ShutdownScope]:
        return [scope for scope in self.owner.findChildren(ShutdownScope) if scope is not self]

    @property
    def busy(self) -> bool:
        return bool(self._entries) or any(
            scope._entries or (scope.stopping and not scope.is_stopped)
            for scope in self._children()
        )

    def request_stop(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        if isinstance(self.owner, QWidget):
            self.owner.setEnabled(False)
        for child in self._children():
            if child.stopping:
                continue
            if isinstance(child.owner, ShutdownOwner):
                child.owner.request_shutdown()
            else:
                child.request_stop()
        for entry in tuple(self._entries.values()):
            if entry.cancel is not None and isValid(entry.worker):
                entry.cancel()
            entry.thread.quit()
        self._timer.setInterval(2)
        self._timer.start()
        self._poll()

    def reopen(self) -> None:
        if self._entries:
            raise RuntimeError("Cannot reuse an owner before its workers have joined")
        self.stopping = False
        self.is_stopped = False
        if isinstance(self.owner, QWidget):
            self.owner.setEnabled(True)

    @Slot()
    def _poll(self) -> None:
        deadline = monotonic() + 0.005
        for entry in tuple(self._entries.values()):
            if entry.persistent and not self.stopping:
                continue
            if entry.joined:
                del self._entries[entry.thread]
                if entry.completed is not None:
                    entry.completed()
                if entry.delete_thread and isValid(entry.thread):
                    entry.thread.deleteLater()
            elif entry.thread.wait(1):
                # Worker results were posted before native join. Return to the
                # GUI loop before finalizing on the next prepared timer tick.
                # No sender() inspection or per-thread Qt connection is needed.
                entry.joined = True
            if monotonic() >= deadline:
                break
        if self.stopping and not self.is_stopped and not self.busy:
            self.is_stopped = True
            self._timer.stop()
            self._completed()
        elif not self.stopping and all(entry.persistent for entry in self._entries.values()):
            self._timer.stop()

    def wait_for_stopped(self) -> None:
        """Last-resort join for aboutToQuit and explicit synchronous teardown.

        Never used by normal close handlers. No event pumping, new Qt objects,
        or new connections; all cancellation and finalizers were prepared earlier.
        """
        for child in self._children():
            if not isValid(child):
                continue
            if isinstance(child.owner, ShutdownOwner):
                child.owner.request_shutdown()
            else:
                child.request_stop()
            child.wait_for_stopped()
        self.request_stop()
        while self._entries:
            for entry in tuple(self._entries.values()):
                while not entry.thread.wait(25):
                    pass
                entry.joined = True
            self._poll()
        self._poll()


class ShutdownOwner:
    """Explicit async production API and blocking compatibility/teardown API."""

    _shutdown_scope: ShutdownScope
    _shutdown_result: int = int(QDialog.DialogCode.Rejected)

    def request_shutdown(self) -> None:
        raise NotImplementedError

    def _shutdown_completed(self) -> None:
        if isinstance(self, QDialog):
            QDialog.done(self, self._shutdown_result)

    def done(self, result: int) -> None:
        if self._shutdown_scope.is_stopped:
            return
        self._shutdown_result = result
        self.request_shutdown()

    def reject(self) -> None:
        self.done(int(QDialog.DialogCode.Rejected))

    def accept(self) -> None:
        self.done(int(QDialog.DialogCode.Accepted))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._shutdown_scope.is_stopped:
            event.accept()
        else:
            self.reject()
            event.ignore()

    def shutdown(self) -> None:
        self.request_shutdown()
        self._shutdown_scope.wait_for_stopped()


def request_shutdown(owner: QObject, *args: object) -> None:
    """Request asynchronous owners, or stop a lightweight non-worker facade."""
    if isinstance(owner, ShutdownOwner):
        owner.request_shutdown(*args)
    else:
        owner.shutdown(*args)  # type: ignore[attr-defined]
