"""One bounded synthetic run, launched explicitly, never part of normal pytest."""

import json
import os
import sys
from pathlib import Path
from threading import Event
from time import monotonic, sleep

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.database.document_catalog import AttachmentSourceSummary
from officeflow.presentation.attachment_browser import AttachmentSourceController


def main():
    expected = (Path(__file__).resolve().parents[2] / ".test-tmp" / "stress-profile").resolve()
    assert os.environ.get("OFFICEFLOW_DATA_DIR")
    assert AppPaths.discover().root == expected
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    owner = QWidget()
    started = monotonic()
    for iteration in range(3000):
        entered = Event()

        def lookup(task, identity, entered=entered):
            entered.set()
            return AttachmentSourceSummary(state="unavailable")

        controller = AttachmentSourceController(lookup, owner)
        controller.request(1, iteration, 0, 0)
        deadline = monotonic() + 5
        while not entered.is_set():
            app.processEvents()
            sleep(0.0001)
            if monotonic() > deadline:
                raise TimeoutError(f"worker start at {iteration}")
        controller.request_shutdown()
        while not controller._shutdown_scope.is_stopped:
            app.processEvents()
            sleep(0.0001)
            if monotonic() > deadline:
                raise TimeoutError(f"worker join at {iteration}")
        assert controller._thread is None and controller._worker is None
        controller.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        if (iteration + 1) % 500 == 0:
            print(
                json.dumps({"completed": iteration + 1, "seconds": monotonic() - started}),
                flush=True,
            )
    assert not owner.findChildren(AttachmentSourceController)
    print(
        json.dumps(
            {
                "result": "passed",
                "iterations": 3000,
                "seconds": monotonic() - started,
                "python": sys.version.split()[0],
                "pyside": pyside_version,
                "profile": str(expected),
                "remaining_controllers": 0,
            }
        ),
        flush=True,
    )
    owner.close()


if __name__ == "__main__":
    main()
