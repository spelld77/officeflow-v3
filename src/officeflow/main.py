from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from officeflow import __version__
from officeflow.application.attachment_search import AttachmentSearchService
from officeflow.application.attachments import AttachmentService
from officeflow.application.exporting import ExportService
from officeflow.application.records import RecordService
from officeflow.application.reminders import ReminderService
from officeflow.application.tasks import TaskService
from officeflow.bootstrap.logging import configure_logging
from officeflow.bootstrap.paths import AppPaths
from officeflow.bootstrap.run_state import RunStateTracker
from officeflow.bootstrap.single_instance import SingleInstanceCoordinator, instance_name
from officeflow.infrastructure.attachments.storage import ManagedAttachmentStorage
from officeflow.infrastructure.backup import BackupError, BackupManager
from officeflow.infrastructure.database.attachment_repository import (
    SqlAlchemyAttachmentRepository,
)
from officeflow.infrastructure.database.attachment_search_repository import (
    SqlAlchemyAttachmentSearchRepository,
)
from officeflow.infrastructure.database.migrate import (
    needs_attachment_search_upgrade,
    upgrade_database,
)
from officeflow.infrastructure.database.record_repository import SqlAlchemyRecordRepository
from officeflow.infrastructure.database.reminder_repository import SqlAlchemyReminderRepository
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository
from officeflow.infrastructure.exports.calendar import ICalendarTaskExporter
from officeflow.infrastructure.exports.excel import ExcelTaskExporter
from officeflow.infrastructure.settings.store import JsonSettingsStore
from officeflow.infrastructure.windows.startup import WindowsStartupManager
from officeflow.presentation.database_upgrade import DatabaseUpgradeDialog
from officeflow.presentation.main_window import MainWindow

logger = logging.getLogger(__name__)


def build_application(
    argv: list[str] | None = None,
    *,
    application: QApplication | None = None,
    desktop_integration: bool = False,
    previous_unclean_shutdown: bool = False,
    on_clean_shutdown: Callable[[], None] | None = None,
    hourly_notifications_enabled: bool = False,
    restore_failure_handler: Callable[[BackupError], str] | None = None,
) -> tuple[QApplication, MainWindow]:
    app = application or QApplication(argv or sys.argv)
    paths = AppPaths.discover()
    paths.ensure_directories()
    configure_logging(paths.log_dir)
    backup_manager = BackupManager(paths)
    def apply_restore() -> None:
        if backup_manager.restore_is_pending:
            def restore(progress: Callable[[str], None]) -> None:
                progress("예약한 백업을 검증하고 복원하고 있습니다. 자료 보호를 위해 종료하지 마세요…")
                backup_manager.apply_pending_restore()
            DatabaseUpgradeDialog(restore, message="예약한 백업을 복원하고 있습니다…").run_upgrade()
        else:
            backup_manager.apply_pending_restore()
    prepare_pending_restore(backup_manager, on_failure=restore_failure_handler, apply_operation=apply_restore)

    settings_store = JsonSettingsStore(paths.settings_file)
    settings = settings_store.load()
    if needs_attachment_search_upgrade(paths.database_file):
        DatabaseUpgradeDialog(lambda progress: upgrade_database(paths.database_file, progress=progress)).run_upgrade()
    else:
        upgrade_database(paths.database_file)
    recovery_warnings = backup_manager.recover_attachment_deletions()
    for warning in recovery_warnings:
        logger.warning(warning)
    engine = create_database_engine(paths.database_file)
    sessions = SessionFactory(engine)
    task_service = TaskService(SqlAlchemyTaskRepository(sessions), timezone=settings.timezone)
    reminder_service = ReminderService(SqlAlchemyReminderRepository(sessions), task_service)
    record_service = RecordService(SqlAlchemyRecordRepository(sessions), task_service)
    attachment_service = AttachmentService(
        SqlAlchemyAttachmentRepository(sessions),
        ManagedAttachmentStorage(paths.attachment_dir),
        task_service,
    )
    export_service = ExportService(
        task_service,
        ExcelTaskExporter(),
        ICalendarTaskExporter(),
    )
    app.setApplicationName("OfficeFlow")
    app.setApplicationDisplayName("OfficeFlow v3")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("OfficeFlow")

    def shutdown() -> None:
        try:
            engine.dispose()
        finally:
            if on_clean_shutdown is not None:
                on_clean_shutdown()

    window = MainWindow(
        settings=settings,
        task_service=task_service,
        reminder_service=reminder_service,
        record_service=record_service,
        attachment_service=attachment_service,
        attachment_search_service=AttachmentSearchService(
            SqlAlchemyAttachmentSearchRepository(sessions)
        ),
        export_service=export_service,
        backup_manager=backup_manager,
        save_settings=settings_store.save,
        on_shutdown=shutdown,
        desktop_integration=desktop_integration,
        previous_unclean_shutdown=previous_unclean_shutdown,
        hourly_notifications_enabled=hourly_notifications_enabled,
    )
    if backup_manager.restore_warnings or recovery_warnings:
        window.statusBar().showMessage(
            "데이터 복구·정리 확인 필요: " + " · ".join(backup_manager.restore_warnings + recovery_warnings),
            15_000,
        )
    return app, window


def prepare_pending_restore(
    manager: BackupManager, *, on_failure: Callable[[BackupError], str] | None = None,
    apply_operation: Callable[[], object] | None = None,
) -> None:
    """Never open the working DB after an unacknowledged restore failure."""
    while True:
        try:
            (apply_operation or manager.apply_pending_restore)()
            return
        except BackupError as error:
            logger.exception("예약된 백업 복원을 적용하지 못했습니다.")
            if manager.restore_is_incomplete:
                raise
            choice = (on_failure or _restore_failure_choice)(error)
            if choice == "retry":
                continue
            if choice == "cancel":
                retained = manager.cancel_pending_restore()
                manager.restore_warnings += (
                    f"복원 예약을 취소하고 현재 자료를 사용합니다. 복원 파일 보존: {retained}",
                )
                return
            raise BackupError("복원을 완료하지 못해 프로그램 시작을 중단했습니다. 현재 자료와 복원 예약은 보존됩니다.") from error


def _restore_failure_choice(error: BackupError) -> str:
    box = QMessageBox()
    box.setWindowTitle("OfficeFlow · 예약된 복원 실패")
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText("백업 복원을 적용하지 못했습니다.\n현재 자료로 조용히 시작하지 않고 선택을 기다립니다.")
    box.setInformativeText(str(error))
    retry = box.addButton("복원 다시 시도", QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton("예약 취소 후 현재 자료 사용", QMessageBox.ButtonRole.ActionRole)
    stop = box.addButton("시작 중단", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(stop)
    box.setEscapeButton(stop)
    app = QApplication.instance()
    previous = app.quitOnLastWindowClosed() if isinstance(app, QApplication) else None
    if isinstance(app, QApplication):
        app.setQuitOnLastWindowClosed(False)
    try:
        box.exec()
    finally:
        if isinstance(app, QApplication) and previous is not None:
            app.setQuitOnLastWindowClosed(previous)
    return "retry" if box.clickedButton() == retry else "cancel" if box.clickedButton() == cancel else "exit"


def main() -> int:
    arguments = sys.argv
    if "--remove-startup" in arguments:
        WindowsStartupManager().set_enabled(False)
        return 0
    if "--smoke-test" in arguments:
        return _run_smoke_test(arguments)
    app = QApplication(arguments)
    app.setApplicationName("OfficeFlow")
    app.setApplicationDisplayName("OfficeFlow v3")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("OfficeFlow")

    paths = AppPaths.discover()
    paths.ensure_directories()
    coordinator = SingleInstanceCoordinator(
        instance_name(paths.root),
        app,
        lock_file=paths.root / "officeflow.lock",
    )
    command = "quick-add" if "--quick-add" in arguments else "activate"
    if not coordinator.acquire():
        return 0 if coordinator.send_message(command) else 1

    run_state = RunStateTracker(paths.running_marker_file)
    previous_unclean_shutdown = run_state.begin()

    try:
        app, window = build_application(
            arguments,
            application=app,
            desktop_integration=True,
            hourly_notifications_enabled=True,
            previous_unclean_shutdown=previous_unclean_shutdown,
            on_clean_shutdown=run_state.mark_clean,
        )
    except Exception as error:
        logger.exception("OfficeFlow 데이터 준비 또는 시작에 실패했습니다.")
        message = "데이터 준비 또는 프로그램 시작을 완료하지 못했습니다. 로그를 확인한 뒤 다시 실행해 주세요."
        if isinstance(error, OSError):
            message += f"\n\n{error}"
        QMessageBox.critical(None, "OfficeFlow 시작 실패", message)
        run_state.mark_clean()
        coordinator.close()
        return 1
    coordinator.messageReceived.connect(window.handle_external_command)
    app.aboutToQuit.connect(window.shutdown)
    app.aboutToQuit.connect(coordinator.close)

    background = "--background" in arguments
    if not background or not window.tray_available:
        window.show()
    if command == "quick-add":
        QTimer.singleShot(0, lambda: window.handle_external_command("quick-add"))
    return app.exec()


def _run_smoke_test(arguments: list[str]) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication(arguments)
    app, window = build_application(arguments, application=app)
    app.processEvents()
    window.shutdown()
    window.close()
    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
