from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from officeflow.bootstrap.logging import configure_logging
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.settings.store import JsonSettingsStore
from officeflow.presentation.main_window import MainWindow


def build_application(argv: list[str] | None = None) -> tuple[QApplication, MainWindow]:
    paths = AppPaths.discover()
    paths.ensure_directories()
    configure_logging(paths.log_dir)

    settings_store = JsonSettingsStore(paths.settings_file)
    settings = settings_store.load()
    upgrade_database(paths.database_file)

    app = QApplication(argv or sys.argv)
    app.setApplicationName("OfficeFlow")
    app.setApplicationDisplayName("OfficeFlow v3")
    app.setOrganizationName("OfficeFlow")

    window = MainWindow(settings=settings, save_settings=settings_store.save)
    return app, window


def main() -> int:
    app, window = build_application()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
