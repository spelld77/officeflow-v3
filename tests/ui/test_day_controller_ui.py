from datetime import date
from threading import Event

from PySide6.QtCore import QObject, QTimer

from officeflow.application.day_overview import DayOverviewResult
from officeflow.presentation.day_controller import DayOverviewController


def test_old_day_result_is_discarded_and_worker_is_released(qtbot):
    started, release = Event(), Event()
    old, current = date(2027, 10, 15), date(2027, 10, 16)

    class Service:
        def query(self, day, *, cancel_requested, **_kwargs):
            if day == old:
                started.set()
                release.wait(2)
            return DayOverviewResult(day, (), True)

    owner = QObject()
    controller = DayOverviewController(Service(), owner)
    delivered = []
    controller.ready.connect(delivered.append)
    try:
        controller.request(old, "UTC", "")
        qtbot.waitUntil(started.is_set)
        controller.request(current, "UTC", "")
        release.set()
        qtbot.waitUntil(lambda: len(delivered) == 1 and controller._thread is None)
        assert delivered[0].day == current
        assert controller._worker is None
    finally:
        release.set()
        controller.shutdown()


def test_controller_recovers_after_query_failure(qtbot):
    class Service:
        failed = False

        def query(self, day, **_kwargs):
            if not self.failed:
                self.failed = True
                raise OSError("조회 실패")
            return DayOverviewResult(day, (), True)

    owner = QObject()
    controller = DayOverviewController(Service(), owner)
    errors, results = [], []
    controller.failed.connect(errors.append)
    controller.ready.connect(results.append)
    try:
        controller.request(date(2027, 10, 15), "UTC", "")
        qtbot.waitUntil(lambda: len(errors) == 1 and controller._thread is None)
        controller.request(date(2027, 10, 15), "UTC", "")
        qtbot.waitUntil(lambda: len(results) == 1)
        assert str(errors[0]) == "조회 실패"
    finally:
        controller.shutdown()


def test_shutdown_handles_finished_slot_running_during_wait(qtbot):
    started, release = Event(), Event()

    class Service:
        def query(self, day, **_kwargs):
            started.set()
            release.wait(2)
            return DayOverviewResult(day, (), True)

    owner = QObject()
    controller = DayOverviewController(Service(), owner)
    controller.request(date(2027, 10, 15), "UTC", "")
    qtbot.waitUntil(started.is_set)
    QTimer.singleShot(0, release.set)
    controller.shutdown()
    assert controller._thread is None and controller._worker is None
    controller.shutdown()
