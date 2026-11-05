"""Capture the existing shutdown status on a small native Windows window."""

import json
import os
from pathlib import Path
from threading import Event
from time import monotonic, sleep

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from officeflow.application.tasks import TaskService
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.latest_job import LatestJob
from officeflow.presentation.main_window import MainWindow
from tests.unit.test_task_service import InMemoryTaskRepository


def main():
    root = Path(__file__).resolve().parents[2]
    scale = os.environ["QT_SCALE_FACTOR"]
    palette_name = os.environ["OFFICEFLOW_QA_PALETTE"]
    expected = (root / ".test-tmp" / f"native-{scale}-{palette_name}").resolve()
    assert AppPaths.discover().root == expected
    app = QApplication([])
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    if palette_name == "dark":
        palette = QPalette(app.palette())
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#202020"))
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
        ):
            palette.setColor(role, QColor("#eeeeee"))
        app.setPalette(palette)
    window = MainWindow(AppSettings(), TaskService(InMemoryTaskRepository()))
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    window.resize(760, 560)
    entered, release = Event(), Event()
    reader = LatestJob(window)

    def work():
        entered.set()
        release.wait(5)

    reader.submit(work)
    window.show()
    deadline = monotonic() + 4
    while not entered.is_set():
        app.processEvents()
        sleep(0.001)
        assert monotonic() < deadline
    window.request_shutdown()
    try:
        for _ in range(10):
            app.processEvents()
            sleep(0.005)
        assert window.isVisible() and not window._shutdown_scope.is_stopped
        assert "안전하게" in window.statusBar().currentMessage()
        assert not window._search.isEnabled()
        destination = root / "artifacts" / "u39-shutdown"
        destination.mkdir(parents=True, exist_ok=True)
        image = destination / f"closing-{scale}-{palette_name}.png"
        assert window.grab().save(str(image))
    finally:
        release.set()
        window.shutdown()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    print(
        json.dumps({"scale": scale, "palette": palette_name, "image": str(image), "passed": True})
    )


if __name__ == "__main__":
    main()
