"""Isolated offscreen rendering using Windows fonts, never an operating profile."""

import os
import sys
import tempfile
from pathlib import Path
from typing import cast

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_SCALE_FACTOR"] = sys.argv[2]

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QColor, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QPushButton

from officeflow.application.attachments import AttachmentService
from officeflow.application.exporting import ExportService
from officeflow.application.tasks import TaskQuery, TaskService
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.attachments.storage import ManagedAttachmentStorage
from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.database.attachment_repository import SqlAlchemyAttachmentRepository
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository
from officeflow.presentation.data_dialog import DataManagementDialog


def settle(dialog):
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(
        lambda: (
            loop.quit()
            if dialog._thread is None and dialog.inspection_panel._thread is None
            else None
        )
    )
    timer.start(10)
    QTimer.singleShot(10000, loop.quit)
    loop.exec()
    timer.stop()
    assert dialog._thread is None and dialog.inspection_panel._thread is None


output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
app = QApplication([])
for filename in ("malgun.ttf", "malgunbd.ttf", "segoeui.ttf"):
    assert (
        QFontDatabase.addApplicationFont(
            str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename)
        )
        >= 0
    )
if sys.argv[3] == "dark":
    palette = app.palette()
    for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
        palette.setColor(role, QColor("#202020"))
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(role, QColor("#000000"))
    app.setPalette(palette)
with tempfile.TemporaryDirectory(prefix="officeflow-reliability-render-") as temporary:
    paths = AppPaths(Path(temporary))
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    engine = create_database_engine(paths.database_file)
    sessions = SessionFactory(engine)
    tasks = TaskService(SqlAlchemyTaskRepository(sessions))
    attachments = AttachmentService(
        SqlAlchemyAttachmentRepository(sessions),
        ManagedAttachmentStorage(paths.attachment_dir),
        tasks,
    )
    manager = BackupManager(paths)
    manager.health.begin("automatic")
    manager.health.finish("automatic", "success")
    manager.health.begin("automatic")
    manager.health.finish(
        "automatic", "failed", "필요한 첨부파일이 누락되었습니다. 자료 검사에서 확인하세요."
    )
    dialog = DataManagementDialog(
        export_service=cast(ExportService, object()),
        backup_manager=manager,
        query=TaskQuery(),
        attachment_service=attachments,
    )
    dialog.resize(500, 480)
    dialog.show()
    settle(dialog)
    for name, index in (
        ("backup", dialog._tools_tab_index),
        ("inspection", dialog._inspection_tab_index),
    ):
        dialog.tabs.setCurrentIndex(index)
        if name == "inspection":
            dialog.inspection_panel.start()
        settle(dialog)
        if name == "backup":
            from PySide6.QtWidgets import QScrollArea

            dialog.findChild(QScrollArea, "dataManagementScroll").ensureWidgetVisible(
                dialog.backup_health_label
            )
            app.processEvents()
        assert dialog.width() <= 520
        if name == "inspection":
            pixel = dialog.inspection_panel.grab().toImage().pixelColor(2, 2)
            assert pixel.lightness() > 180, f"inspection background is dark: {pixel.name()}"
            assert (
                dialog.inspection_panel.results.viewport()
                .grab()
                .toImage()
                .pixelColor(2, 2)
                .lightness()
                > 180
            )
        for widget in dialog.findChildren(QPushButton):
            # Backup actions live in a scroll area and may be beyond its viewport.
            if (
                widget.isVisible()
                and name == "inspection"
                and not dialog.inspection_panel.isAncestorOf(widget)
            ):
                assert dialog.rect().contains(widget.mapTo(dialog, widget.rect().bottomRight()))
        assert dialog.grab().save(str(output / f"{name}.png"))
    dialog.shutdown()
    engine.dispose()
print(f"rendered {sys.argv[2]} {sys.argv[3]}")
