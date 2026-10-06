from datetime import UTC, datetime
from threading import Event
from typing import cast

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from officeflow.application.reminder_preview import ReminderPreviewResult, ReminderPreviewService
from officeflow.presentation.reminder_preview import ReminderPreviewController


def controller_for(qtbot, service):
    parent = QWidget()
    qtbot.addWidget(parent)
    return ReminderPreviewController(cast(ReminderPreviewService, service), parent), parent


def request(controller):
    now = datetime.now(UTC)
    controller.request(now.date(), now.date(), "Asia/Seoul", now)


def test_preview_results_and_followup_requests_stay_on_ui_thread(qtbot, qapp):
    class Service:
        def query(self, *_args, **_kwargs):
            return ReminderPreviewResult((), True)

    controller, _parent = controller_for(qtbot, Service())
    observed = []
    controller.ready.connect(
        lambda _result: observed.append(QThread.currentThread() == qapp.thread()),
        Qt.ConnectionType.DirectConnection,
    )
    original_start = controller._start

    def start():
        observed.append(QThread.currentThread() == qapp.thread())
        original_start()

    controller._start = start
    try:
        for _ in range(40):
            request(controller)
            qtbot.waitUntil(lambda: controller._thread is None)
        assert len(observed) == 120
        assert all(observed)
    finally:
        controller.shutdown()


def test_preview_failures_are_reported_on_ui_thread(qtbot, qapp):
    class Service:
        def query(self, *_args, **_kwargs):
            raise OSError("격리된 조회 실패")

    controller, _parent = controller_for(qtbot, Service())
    observed = []
    controller.failed.connect(
        lambda error: observed.append((str(error), QThread.currentThread() == qapp.thread())),
        Qt.ConnectionType.DirectConnection,
    )
    try:
        for _ in range(5):
            request(controller)
            qtbot.waitUntil(lambda: controller._thread is None)
        assert observed == [("격리된 조회 실패", True)] * 5
    finally:
        controller.shutdown()


def test_preview_canceled_generation_cannot_overwrite_latest_result(qtbot):
    entered = Event()

    class Service:
        calls = 0

        def query(self, *_args, cancel_requested, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                while not cancel_requested():
                    Event().wait(0.001)
                return ReminderPreviewResult((), True)
            return ReminderPreviewResult((), False)

    controller, _parent = controller_for(qtbot, Service())
    observed = []
    controller.ready.connect(observed.append)
    try:
        request(controller)
        qtbot.waitUntil(entered.is_set)
        request(controller)
        qtbot.waitUntil(lambda: controller._thread is None)
        assert observed == [ReminderPreviewResult((), False)]
    finally:
        controller.shutdown()


def test_preview_shutdown_joins_worker_and_ignores_late_result(qtbot):
    entered = Event()

    class Service:
        def query(self, *_args, cancel_requested, **_kwargs):
            entered.set()
            while not cancel_requested():
                Event().wait(0.001)
            return ReminderPreviewResult((), True)

    controller, _parent = controller_for(qtbot, Service())
    observed = []
    controller.ready.connect(observed.append)
    request(controller)
    qtbot.waitUntil(entered.is_set)
    thread = controller._thread
    controller.shutdown()
    assert thread is not None and (not isValid(thread) or not thread.isRunning())
    qtbot.waitUntil(lambda: controller._thread is None)
    assert observed == []
