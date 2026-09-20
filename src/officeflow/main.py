from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from officeflow import __version__
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
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.record_repository import SqlAlchemyRecordRepository
from officeflow.infrastructure.database.reminder_repository import SqlAlchemyReminderRepository
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository
from officeflow.infrastructure.exports.calendar import ICalendarTaskExporter
from officeflow.infrastructure.exports.excel import ExcelTaskExporter
from officeflow.infrastructure.migration.legacy_v26 import LegacyV26Migration
from officeflow.infrastructure.settings.store import JsonSettingsStore
from officeflow.infrastructure.windows.startup import WindowsStartupManager
from officeflow.presentation.main_window import MainWindow

logger = logging.getLogger(__name__)


def build_application(
    argv: list[str] | None = None,
    *,
    application: QApplication | None = None,
    desktop_integration: bool = False,
    previous_unclean_shutdown: bool = False,
    on_clean_shutdown: Callable[[], None] | None = None,
) -> tuple[QApplication, MainWindow]:
    paths = AppPaths.discover()
    paths.ensure_directories()
    configure_logging(paths.log_dir)
    backup_manager = BackupManager(paths)
    try:
        backup_manager.apply_pending_restore()
    except BackupError:
        logger.exception("예약된 백업 복원을 적용하지 못했습니다.")

    settings_store = JsonSettingsStore(paths.settings_file)
    settings = settings_store.load()
    upgrade_database(paths.database_file)
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
    migration_service = LegacyV26Migration(
        paths,
        backup_manager,
        timezone=settings.timezone,
    )

    app = application or QApplication(argv or sys.argv)
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
        export_service=export_service,
        backup_manager=backup_manager,
        migration_service=migration_service,
        save_settings=settings_store.save,
        on_shutdown=shutdown,
        desktop_integration=desktop_integration,
        previous_unclean_shutdown=previous_unclean_shutdown,
    )
    return app, window


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

    app, window = build_application(
        arguments,
        application=app,
        desktop_integration=True,
        previous_unclean_shutdown=previous_unclean_shutdown,
        on_clean_shutdown=run_state.mark_clean,
    )
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
