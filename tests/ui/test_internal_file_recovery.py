"""Recovery starts after construction, off the UI thread; safe shutdown owns workers."""

from threading import Event, get_ident
from time import sleep

from PySide6.QtCore import QThread, QTimer, Slot

from officeflow.application.attachment_documents import InternalRecoveryReport
from officeflow.application.tasks import TaskDraft
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow


def test_periodic_recovery_completion_is_gui_owned_and_stops_on_shutdown(qtbot, task_service):
    calls = []

    class CheckedWindow(MainWindow):
        @Slot(object)
        def _internal_recovery_succeeded(self, result):
            assert QThread.currentThread() == self.thread()
            super()._internal_recovery_succeeded(result)

    def recover(canceled):
        calls.append(True)
        return InternalRecoveryReport(0, 0, ())

    window = CheckedWindow(AppSettings(), task_service, internal_recovery=recover)
    qtbot.addWidget(window)
    window.show()
    for number in range(1, 9):
        window._internal_recovery_timer.start(0)
        qtbot.waitUntil(
            lambda expected=number: (
                len(calls) >= expected and window._internal_recovery_thread is None
            )
        )
        assert window._internal_recovery_timer.isActive()
    window.shutdown()
    assert not window._internal_recovery_timer.isActive()
    before = len(calls)
    qtbot.wait(1100)
    assert len(calls) == before and window._internal_recovery_thread is None


def test_internal_recovery_does_not_block_first_window_task_input_or_ui_ticks(qtbot, task_service):
    entered, release = Event(), Event()
    ids, ticks = [], []

    def recover(canceled):
        ids.append(get_ident())
        entered.set()
        while not release.wait(0.005) and not canceled():
            continue
        return InternalRecoveryReport(1, 0, ())

    window = MainWindow(AppSettings(), task_service, internal_recovery=recover)
    qtbot.addWidget(window)
    assert not entered.is_set()  # Construction/preflight did not hash/wait.
    window.show()
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    try:
        qtbot.waitUntil(entered.is_set)
        assert window.isVisible() and window._search.isEnabled()
        assert task_service.create(TaskDraft(title="복구 중에도 업무 입력")).id
        qtbot.waitUntil(lambda: len(ticks) >= 3)
        assert ids != [get_ident()]
        release.set()
        qtbot.waitUntil(lambda: window._internal_recovery_thread is None)
        assert "완료 확인 1개" in window.statusBar().currentMessage()
    finally:
        release.set()
        timer.stop()
        window.shutdown()


def test_internal_recovery_cancel_finishes_before_database_owner_disposal(qtbot, task_service):
    entered, finished = Event(), Event()
    closed, ticks = [], []

    def recover(canceled):
        entered.set()
        while not canceled():
            sleep(0.005)
        sleep(0.1)
        finished.set()
        return InternalRecoveryReport(0, 1, ())

    def shutdown():
        closed.append(finished.is_set())

    window = MainWindow(
        AppSettings(), task_service, internal_recovery=recover, on_shutdown=shutdown
    )
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(entered.is_set)
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    window.shutdown()
    timer.stop()
    assert closed == [True] and len(ticks) >= 3
    assert window._internal_recovery_worker is None and window._internal_recovery_thread is None
