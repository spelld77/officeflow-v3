from __future__ import annotations

from collections.abc import Callable
from threading import Event

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import QProgressDialog, QWidget
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


def finish_thread(
    thread: QThread,
    *,
    parent: QWidget | None = None,
    label: str = "진행 중인 작업을 안전하게 마무리하고 있습니다…",
) -> None:
    """Wait responsively; never terminate file/DB workers or dispose their owners early."""
    if not thread.isRunning():
        return
    loop = QEventLoop()
    progress = QProgressDialog(label, "", 0, 0, parent)
    progress.setObjectName("safeShutdownProgress")
    progress.setWindowTitle("OfficeFlow · 안전한 마무리")
    progress.setWindowModality(Qt.WindowModality.NonModal)
    progress.setCancelButton(None)
    progress.setMinimumDuration(500)
    progress.setAutoClose(False)
    progress.setValue(0)
    thread.finished.connect(loop.quit)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(
        lambda: progress.setLabelText(
            label
            + "\n디스크나 보안 검사 응답을 기다리고 있습니다. 자료 보호를 위해 강제 종료하지 마세요."
        )
    )
    timer.start(5_000)
    try:
        # The worker can stop between the initial check and this connection.
        if thread.isRunning():
            loop.exec()
        # aboutToQuit can stop a nested loop before a blocked OS call returns.
        # Use short waits in that case, still keeping ownership until it stops.
        while isValid(thread) and thread.isRunning():
            thread.wait(25)
            QCoreApplication.processEvents()
    finally:
        timer.stop()
        progress.close()
        progress.deleteLater()
