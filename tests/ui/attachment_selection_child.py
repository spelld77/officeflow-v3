"""Isolated native Qt stress: a crash must not bring down the pytest parent."""

from __future__ import annotations

import faulthandler
import sys
from pathlib import Path
from threading import Event

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from officeflow.application.attachment_export import AttachmentExportService
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
from tests.integration.test_document_lifecycle import lifecycle, original
from tests.ui.test_document_lifecycle_ui import records


def main() -> None:
    faulthandler.enable()
    app = QApplication([])
    assert app.platformName() == "windows"
    root = Path(sys.argv[1])
    root.mkdir()
    with lifecycle(root) as (env, _service):
        source = original(env, _service)
        for index in range(8):
            path = root / f"input-{index}.txt"
            path.write_bytes(f"file {index}".encode())
            item = env.attachments.attach(source.task_id, path)
            env.attachments.rename_attachment(
                item.id,
                f"{index}-" + ("매우 긴 업무 첨부 이름 " * 10) + ".txt",
                expected_revision=item.metadata_revision,
            )
        exports = AttachmentExportService(
            env.attachments, ExternalExportStorage(root / "journal")
        )
        for _window in range(2):
            dialog = records(env, source, exports)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            dialog.resize(600, 540)
            dialog.show()
            listing = dialog.attachment_list
            for _attempt in range(200):
                if dialog._sample_thread is not None:
                    break
                QTest.qWait(10)
            thread = dialog._sample_thread
            assert thread is not None
            for _iteration in range(40):
                listing.clearSelection()
                listing.setCurrentRow(0)
                listing.item(1).setSelected(True)
                listing.item(2).setSelected(True)
                assert len(listing.selectedItems()) == 3
                QTest.qWait(110)
                assert dialog._sample_thread is thread
                listing.item(3).setSelected(True)
                listing.item(0).setSelected(False)
                QTest.qWait(5)
            assert thread.isRunning()
            dialog.shutdown()
            assert dialog._sample_thread is None and not thread.isRunning()
            dialog.close()
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

        # Closing during a sample must cancel/join before repositories are disposed.
        started, canceled = Event(), Event()
        quick_samples = env.attachments.quick_samples

        def slow_samples(items, *, cancel_requested):
            started.set()
            for _attempt in range(500):
                if cancel_requested():
                    canceled.set()
                    return ()
                canceled.wait(0.01)
            return quick_samples(items, cancel_requested=cancel_requested)

        env.attachments.quick_samples = slow_samples
        dialog = records(env, source, exports)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        dialog.show()
        for _attempt in range(100):
            if started.is_set():
                break
            QTest.qWait(10)
        assert started.is_set()
        thread = dialog._sample_thread
        dialog.shutdown()
        assert canceled.is_set() and thread is not None and not thread.isRunning()
        dialog.close()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    print("NATIVE_SELECTION_AND_SHUTDOWN_OK", flush=True)


if __name__ == "__main__":
    main()
