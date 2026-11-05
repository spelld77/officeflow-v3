from itertools import pairwise
from threading import Event, get_ident
from time import monotonic, sleep

import pytest
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Slot
from PySide6.QtWidgets import QDialog, QWidget
from shiboken6 import isValid

from officeflow.infrastructure.database.document_catalog import AttachmentSourceSummary
from officeflow.presentation import background
from officeflow.presentation.attachment_browser import AttachmentSourceController
from officeflow.presentation.background import OperationWorker, ShutdownOwner, ShutdownScope
from officeflow.presentation.latest_job import LatestJob


class ClosingDialog(ShutdownOwner, QDialog):
    def __init__(self):
        super().__init__()
        self._shutdown_scope = ShutdownScope(self, self._shutdown_completed)

    def request_shutdown(self):
        self._shutdown_scope.request_stop()


def test_close_returns_before_native_cleanup_and_keeps_owner_alive(qtbot):
    dialog = ClosingDialog()
    qtbot.addWidget(dialog)
    entered, release, cleanup_entered, cleanup_done = (Event() for _ in range(4))
    thread = QThread(dialog)

    def operation(canceled):
        entered.set()
        release.wait(2)

    worker = OperationWorker(operation)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
    thread.finished.connect(worker.deleteLater)

    def cleanup():
        cleanup_entered.set()
        sleep(0.12)
        cleanup_done.set()

    thread.finished.connect(cleanup, Qt.ConnectionType.DirectConnection)
    dialog._shutdown_scope.track(thread, worker, cancel=worker.cancel)
    dialog.show()
    thread.start()
    qtbot.waitUntil(entered.is_set)
    closed = []
    dialog.finished.connect(closed.append)
    dialog.reject()
    dialog.reject()
    assert dialog.isVisible() and not dialog._shutdown_scope.is_stopped
    assert isValid(thread) and isValid(worker)
    release.set()
    qtbot.waitUntil(cleanup_entered.is_set)
    assert not closed and not cleanup_done.is_set()
    qtbot.waitUntil(lambda: dialog._shutdown_scope.is_stopped)
    assert cleanup_done.is_set() and closed == [int(QDialog.DialogCode.Rejected)]
    dialog.reject()
    assert closed == [int(QDialog.DialogCode.Rejected)]


def test_parent_waits_for_child_scope_before_finalizing_resources(qtbot):
    owner = QWidget()
    qtbot.addWidget(owner)
    order = []
    parent = ShutdownScope(owner, lambda: order.append("database closed"))
    child = QObject(owner)
    child_scope = ShutdownScope(child, lambda: order.append("child stopped"))
    entered, released = Event(), Event()
    thread = QThread(child)

    def run(canceled):
        entered.set()
        released.wait(2)

    worker = OperationWorker(run)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
    thread.finished.connect(worker.deleteLater)
    child_scope.track(thread, worker)
    thread.start()
    qtbot.waitUntil(entered.is_set)
    parent.request_stop()
    assert not order
    released.set()
    qtbot.waitUntil(lambda: parent.is_stopped)
    assert order == ["child stopped", "database closed"]


def test_source_shutdown_discards_pending_reads_without_new_qt_objects(qtbot, monkeypatch):
    owner = QWidget()
    qtbot.addWidget(owner)
    entered, release = Event(), Event()
    calls = []

    def lookup(task, identity):
        calls.append(identity)
        entered.set()
        release.wait(2)
        return AttachmentSourceSummary(state="unavailable")

    controller = AttachmentSourceController(lookup, owner)
    controller.request(1, 1, 0, 0)
    qtbot.waitUntil(entered.is_set)
    assert not controller._shutdown_scope._timer.isActive()
    controller.request(1, 2, 0, 0)
    updates = []
    controller.updated.connect(lambda: updates.append(True))

    def unexpected_timer(*args):
        raise AssertionError("Shutdown created a new Qt timer")

    monkeypatch.setattr(background, "QTimer", unexpected_timer)

    def unexpected_sender(*args):
        raise AssertionError("Shutdown inspected QObject.sender()")

    monkeypatch.setattr(ShutdownScope, "sender", unexpected_sender)
    started = monotonic()
    controller.request_shutdown()
    controller.request_shutdown()
    assert monotonic() - started < 0.1
    assert controller._thread is not None and controller._worker is not None
    release.set()
    qtbot.waitUntil(lambda: controller._shutdown_scope.is_stopped)
    assert calls == [1] and not updates and controller._thread is None


def test_result_delivery_precedes_join_callback(qtbot):
    order = []

    class Receiver(QObject):
        @Slot(object)
        def receive(self, value):
            order.append(value)

    owner = Receiver()
    scope = ShutdownScope(owner, lambda: order.append("stopped"))
    thread = QThread(owner)
    worker = OperationWorker(lambda canceled: 42)
    worker.moveToThread(thread)
    worker.succeeded.connect(owner.receive, Qt.ConnectionType.QueuedConnection)
    worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
    thread.finished.connect(worker.deleteLater)
    thread.started.connect(worker.run)
    scope.track(thread, worker, lambda: order.append("joined"))
    thread.start()
    qtbot.waitUntil(lambda: "joined" in order)
    scope.request_stop()
    assert order == [42, "joined", "stopped"]


def test_latest_job_finalizer_runs_once_on_worker_thread_during_blocking_fallback(qtbot):
    job = LatestJob()
    entered = Event()
    identities = []

    def work():
        identities.append(get_ident())
        entered.set()

    job.submit(work)
    qtbot.waitUntil(entered.is_set)
    main_thread = get_ident()
    job.shutdown(lambda: identities.append(get_ident()))
    job.shutdown(lambda: identities.append(main_thread))
    assert len(identities) == 2 and identities[0] == identities[1] != main_thread
    assert job._thread is None and job._reader is None


def test_already_finished_worker_can_be_stopped_idempotently(qtbot):
    owner = QWidget()
    qtbot.addWidget(owner)
    controller = AttachmentSourceController(
        lambda task, identity: AttachmentSourceSummary(state="unavailable"), owner
    )
    controller.request(1, 1, 0, 0)
    qtbot.waitUntil(lambda: controller.result is not None)
    controller.request_shutdown()
    qtbot.waitUntil(lambda: controller._shutdown_scope.is_stopped)
    controller.request_shutdown()
    assert not controller._shutdown_scope.busy


def test_async_shutdown_preserves_gui_heartbeat(qtbot):
    dialog = ClosingDialog()
    qtbot.addWidget(dialog)
    ticks = []
    heartbeat = QTimer(dialog)
    heartbeat.setInterval(10)
    heartbeat.timeout.connect(lambda: ticks.append(monotonic()))
    entered = Event()
    thread = QThread(dialog)

    def operation(canceled):
        entered.set()
        while not canceled():
            sleep(0.002)
        sleep(0.5)

    worker = OperationWorker(operation)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
    thread.finished.connect(worker.deleteLater)
    dialog._shutdown_scope.track(thread, worker, cancel=worker.cancel)
    heartbeat.start()
    dialog.show()
    thread.start()
    qtbot.waitUntil(entered.is_set)
    dialog.reject()
    qtbot.waitUntil(lambda: dialog._shutdown_scope.is_stopped)
    heartbeat.stop()
    assert len(ticks) >= 10
    assert max(b - a for a, b in pairwise(ticks)) < 0.2


def test_main_window_defers_database_callback_until_worker_and_child_stop(qtbot):
    from officeflow.application.tasks import TaskService
    from officeflow.infrastructure.settings.store import AppSettings
    from officeflow.presentation.main_window import MainWindow
    from tests.unit.test_task_service import InMemoryTaskRepository

    entered, release = Event(), Event()
    order = []
    window = MainWindow(
        AppSettings(),
        TaskService(InMemoryTaskRepository()),
        on_shutdown=lambda: order.append("database closed"),
    )
    qtbot.addWidget(window)
    reader = LatestJob(window)

    def run():
        entered.set()
        release.wait(2)
        order.append("worker returned")

    reader.submit(run)
    qtbot.waitUntil(entered.is_set)
    reader.request_shutdown(lambda: order.append("worker finalizer"))
    window.request_shutdown()
    window.request_shutdown()
    assert not order
    release.set()
    qtbot.waitUntil(lambda: window._shutdown_scope.is_stopped)
    assert order == ["worker returned", "worker finalizer", "database closed"]


def test_cleanup_failure_does_not_mark_profile_clean(tmp_path, qtbot, qapp, monkeypatch):
    import officeflow.main as main_module

    profile = tmp_path / "synthetic-profile"
    monkeypatch.setenv("OFFICEFLOW_DATA_DIR", str(profile))
    cleaned = []
    _app, window = main_module.build_application(
        application=qapp, on_clean_shutdown=lambda: cleaned.append(True)
    )
    qtbot.addWidget(window)

    def fail_close(self):
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(main_module.FileMaintenance, "close", fail_close)
    with pytest.raises(OSError, match="synthetic cleanup failure"):
        window.shutdown()
    assert not cleaned


@pytest.mark.parametrize("kind", ["history", "snapshot"])
def test_child_close_cannot_start_new_read_during_parent_shutdown(qtbot, kind):
    from officeflow.presentation.task_history_dialog import TaskHistoryDialog, _ReadDialog
    from officeflow.presentation.task_snapshot_dialog import SnapshotDetailDialog

    class HistoryOwner(TaskHistoryDialog):
        def __init__(self):
            _ReadDialog.__init__(self)
            self._unsubscribe = lambda: None
            self._children = []

    class SnapshotOwner(SnapshotDetailDialog):
        def __init__(self):
            _ReadDialog.__init__(self)
            self.export = None
            self._info_dialogs = []

    parent = HistoryOwner() if kind == "history" else SnapshotOwner()
    qtbot.addWidget(parent)
    child = _ReadDialog(parent)
    if kind == "history":
        parent._children.append(child)
    else:
        parent._info_dialogs.append(child)
    operations = []
    child.finished.connect(
        lambda result: parent.run(lambda: operations.append(True), lambda value: None)
    )
    parent.request_shutdown()
    qtbot.waitUntil(lambda: parent._shutdown_scope.is_stopped)
    assert not operations
