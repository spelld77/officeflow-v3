from threading import Event, get_ident
from time import sleep

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QPushButton
from shiboken6 import isValid

from officeflow.application.exporting import ExportCanceledError
from officeflow.application.tasks import TaskQuery
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.presentation.data_dialog import DataManagementDialog
from officeflow.presentation.database_upgrade import DatabaseUpgradeDialog


def _dialog(tmp_path, qtbot):
    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    dialog = DataManagementDialog(
        export_service=object(), backup_manager=BackupManager(paths), query=TaskQuery()
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog._thread is None)
    return dialog


def test_user_cancel_reaches_busy_worker_and_removes_progress(tmp_path, qtbot):
    dialog = _dialog(tmp_path, qtbot)
    entered = Event()

    def export(canceled, report):
        report(1234, 0)
        entered.set()
        while not canceled():
            sleep(0.005)
        raise ExportCanceledError("내보내기를 취소했습니다.")

    dialog._start("내보내기", lambda _canceled: None, str, progress_operation=export)
    qtbot.waitUntil(lambda: entered.is_set() and "1,234" in dialog.status_label.text())
    progress = dialog._progress
    assert progress is not None and not progress.isModal()
    next(button for button in progress.findChildren(QPushButton) if button.text() == "취소").click()
    qtbot.waitUntil(lambda: dialog._thread is None)
    qtbot.waitUntil(lambda: not isValid(progress))
    assert "취소" in dialog.status_label.text() and dialog.isVisible()
    dialog.shutdown()


def test_data_owner_shutdown_keeps_ui_responsive_until_worker_finishes(tmp_path, qtbot):
    dialog = _dialog(tmp_path, qtbot)
    entered, finished = Event(), Event()

    def operation(canceled):
        entered.set()
        while not canceled():
            sleep(0.005)
        sleep(0.12)
        finished.set()
        raise ExportCanceledError("취소됨")

    dialog._start("작업 중", operation, str)
    qtbot.waitUntil(entered.is_set)
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    dialog.shutdown()
    timer.stop()
    assert finished.is_set() and len(ticks) >= 3
    assert dialog._thread is None and not dialog.isVisible()


def test_startup_progress_runs_work_off_ui_thread_and_waits_until_done(qtbot):
    ticks, thread_ids = [], []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(True))

    def operation(progress):
        thread_ids.append(get_ident())
        progress("복원 검증 중")
        sleep(0.15)

    dialog = DatabaseUpgradeDialog(operation, message="복원 중")
    qtbot.addWidget(dialog)
    timer.start()
    dialog.run_upgrade()
    timer.stop()
    assert thread_ids != [get_ident()] and len(ticks) >= 3
    assert not dialog.worker.isRunning() and dialog.label.text() == "복원 검증 중"
