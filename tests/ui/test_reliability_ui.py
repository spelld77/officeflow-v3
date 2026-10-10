from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtWidgets import QPushButton

from officeflow.application.exporting import ExportService
from officeflow.application.tasks import TaskQuery
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.data_dialog import DataManagementDialog
from officeflow.presentation.main_window import MainWindow


def test_persistent_backup_warning_and_data_inspection_tab(qtbot, tmp_path, task_service):
    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    manager = BackupManager(paths)
    manager.health.begin("automatic")
    manager.health.finish("automatic", "failed", "test safe reason")
    window = MainWindow(
        AppSettings(automatic_backup_enabled=False), task_service, backup_manager=manager
    )
    qtbot.addWidget(window)
    window.show()
    button = window.findChild(QPushButton, "backupHealthWarning")
    assert button is not None and button.isVisible()
    dialog = DataManagementDialog(
        export_service=cast(ExportService, object()), backup_manager=manager, query=TaskQuery()
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog._thread is None)
    assert "test safe reason" in dialog.backup_health_label.text()
    assert dialog.tabs.tabText(dialog._inspection_tab_index) == "자료 검사"
    dialog._acknowledge_backup_health()
    window._refresh_backup_health()
    assert not button.isVisible()
    dialog.inspection_panel.start()
    qtbot.waitUntil(lambda: dialog.inspection_panel._thread is None)
    assert "확인 0개" in dialog.inspection_panel.summary.text()
    dialog.shutdown()
    window.shutdown()


def test_inspection_cancel_and_shutdown_keeps_timer_responsive(qtbot):
    from datetime import UTC, datetime
    from threading import Event

    from PySide6.QtCore import QTimer

    from officeflow.application.data_inspection import InspectionReport
    from officeflow.presentation.data_inspection import DataInspectionPanel

    began = Event()
    finished = Event()

    class Service:
        def run(self, canceled, progress):
            began.set()
            while not canceled():
                Event().wait(0.005)
            finished.set()
            return InspectionReport(0, 1, {}, (), 0, True, datetime.now(UTC))

    panel = DataInspectionPanel(lambda: cast(object, Service()))
    qtbot.addWidget(panel)
    panel.show()
    ticks = []
    timer = QTimer(panel)
    timer.setInterval(1)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    panel.start()
    qtbot.waitUntil(lambda: began.is_set() and len(ticks) >= 3)
    panel.shutdown()
    assert finished.is_set() and panel._thread is None
    timer.stop()


def test_unhandled_error_notice_survives_transient_status(qtbot, task_service):
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.show()
    window.diagnosticError.emit("safe error message")
    qtbot.waitUntil(lambda: window._diagnostic_warning_button.isVisible())
    window.statusBar().showMessage("other normal message", 1)
    qtbot.wait(5)
    assert window._diagnostic_warning_button.isVisible()
    window.shutdown()


@pytest.mark.parametrize("scale", ["1", "1.5", "2"])
@pytest.mark.parametrize("palette", ["light", "dark"])
def test_reliability_small_window_render(scale, palette):
    script = Path(__file__).with_name("reliability_visual_child.py")
    output = (
        Path(__file__).resolve().parents[2]
        / "artifacts"
        / "reliability-visual"
        / f"{palette}-{scale}"
    )
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), str(output), scale, palette],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
