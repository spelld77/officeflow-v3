"""Native Qt image lifetime stress and isolated memory/GUI measurements."""

from __future__ import annotations

import ctypes
import faulthandler
import json
import sys
from pathlib import Path
from time import perf_counter

from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from officeflow.application.tasks import TaskDraft
from officeflow.presentation.attachment_thumbnails import AttachmentImageCoordinator
from officeflow.presentation.record_dialog import TaskRecordsDialog
from tests.integration.test_attachment_search import search_environment


def memory() -> tuple[int, int]:
    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("faults", ctypes.c_ulong),
            *[
                (name, ctypes.c_size_t)
                for name in (
                    "peak_ws",
                    "ws",
                    "peak_paged",
                    "paged",
                    "peak_nonpaged",
                    "nonpaged",
                    "pagefile",
                    "peak_pagefile",
                    "private",
                )
            ],
        ]

    value = Counters()
    value.cb = ctypes.sizeof(value)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    assert psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb)
    return value.ws, value.peak_ws


def wait_for(predicate, seconds=10):
    until = perf_counter() + seconds
    while not predicate():
        assert perf_counter() < until, "image completion timeout"
        QTest.qWait(10)


def main() -> None:
    faulthandler.enable()
    app = QApplication([])
    assert app.platformName() == "windows"
    root = Path(sys.argv[1])
    root.mkdir()
    with search_environment(root) as env:
        task = env.tasks.create(TaskDraft(title="native image lifetime test"))
        size = (int(sys.argv[2]), int(sys.argv[2])) if len(sys.argv) > 2 else (1920, 1080)
        for index, suffix in enumerate(("png", "jpg", "bmp") * 4):
            image = QImage(*size, QImage.Format.Format_ARGB32)
            image.fill(QColor.fromHsv(index * 30, 170, 220))
            path = root / f"{index}-capture.{suffix}"
            assert image.save(str(path))
            env.attachments.attach(task.id, path)
        del image
        coordinator = AttachmentImageCoordinator()
        base, _peak = memory()
        measurements = []
        gaps = []
        last = [perf_counter()]
        timer = QTimer()

        def pulse():
            now = perf_counter()
            gaps.append(now - last[0])
            last[0] = now

        timer.timeout.connect(pulse)
        timer.start(10)
        try:
            for cycle in range(6):
                dialogs = []
                for _window in range(2):
                    dialog = TaskRecordsDialog(
                        task,
                        task_service=env.tasks,
                        record_service=env.records,
                        attachment_service=env.attachments,
                        initial_tab="attachments",
                        image_coordinator=coordinator,
                    )
                    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
                    dialog.resize(500 if cycle % 2 else 620, 540)
                    dialog.show()
                    dialog.thumbnail_view_button.click()
                    dialogs.append(dialog)
                wait_for(lambda: coordinator.cache_bytes > 0)
                for iteration in range(12):
                    for dialog in dialogs:
                        listing = dialog.attachment_list
                        listing.setCurrentRow(iteration % listing.count())
                        listing.item((iteration + 1) % listing.count()).setSelected(True)
                        listing.item((iteration + 2) % listing.count()).setSelected(True)
                        assert len(listing.selectedItems()) >= 2
                        dialog.list_view_button.click()
                        dialog.thumbnail_view_button.click()
                        listing.scrollToBottom() if iteration % 2 else listing.scrollToTop()
                    QTest.qWait(12)
                for dialog in dialogs:
                    dialog.select_attachment(
                        dialog.attachment_list.item(0).data(Qt.ItemDataRole.UserRole)
                    )
                    dialog.image_expand_action.trigger()
                wait_for(
                    lambda windows=tuple(dialogs): all(
                        d._image_preview and not d._image_preview._image.isNull() for d in windows
                    )
                )
                measurements.append(
                    {
                        "cache_bytes": coordinator.cache_bytes,
                        "cache_entries": coordinator.cache_entries,
                        "working_set": memory()[0],
                    }
                )
                assert coordinator.cache_bytes <= coordinator.MAX_CACHE_BYTES
                for dialog in dialogs:
                    dialog.shutdown()
                    dialog.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                assert coordinator.cache_bytes == 0
                wait_for(lambda: not coordinator.busy)
                measurements[-1]["closed_working_set"] = memory()[0]
            assert gaps
        finally:
            timer.stop()
            coordinator.shutdown()
        final, peak = memory()
        print(
            json.dumps(
                {
                    "base_working_set": base,
                    "peak_working_set": peak,
                    "final_working_set": final,
                    "max_gui_gap_ms": round(max(gaps) * 1000, 1),
                    "decode_count": coordinator.decode_count,
                    "read_count": coordinator.read_count,
                    "cycles": measurements,
                }
            ),
            flush=True,
        )
    print("NATIVE_THUMBNAILS_OK", flush=True)


if __name__ == "__main__":
    main()
