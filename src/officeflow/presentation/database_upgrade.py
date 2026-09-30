from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QProgressBar, QVBoxLayout


class UpgradeWorker(QThread):
    progress = Signal(str)

    def __init__(self, operation: Callable[[Callable[[str], None]], None], parent: QDialog) -> None:
        super().__init__(parent)
        self._operation = operation
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            self._operation(self.progress.emit)
        except Exception as error:
            self.error = error


class DatabaseUpgradeDialog(QDialog):
    """Show responsive, non-cancellable progress before the DB is opened."""

    def __init__(self, operation: Callable[[Callable[[str], None]], None]) -> None:
        super().__init__()
        self.setWindowTitle("OfficeFlow · 데이터 준비")
        self.resize(440, 150)
        root = QVBoxLayout(self)
        self.label = QLabel("첨부파일 검색을 준비하고 있습니다…")
        self.label.setWordWrap(True)
        root.addWidget(self.label)
        bar = QProgressBar()
        bar.setRange(0, 0)
        root.addWidget(bar)
        root.addWidget(QLabel("준비가 끝나면 OfficeFlow가 열립니다."))
        self.worker = UpgradeWorker(operation, self)
        self.worker.progress.connect(self.label.setText)
        self.worker.finished.connect(self.accept)

    def reject(self) -> None:
        if not self.worker.isRunning():
            super().reject()

    def run_upgrade(self) -> None:
        application = QApplication.instance()
        previous = (
            application.quitOnLastWindowClosed() if isinstance(application, QApplication) else None
        )
        if isinstance(application, QApplication):
            application.setQuitOnLastWindowClosed(False)
        try:
            self.worker.start()
            self.exec()
            self.worker.wait()
        finally:
            if isinstance(application, QApplication) and previous is not None:
                application.setQuitOnLastWindowClosed(previous)
        if self.worker.error is not None:
            raise self.worker.error
