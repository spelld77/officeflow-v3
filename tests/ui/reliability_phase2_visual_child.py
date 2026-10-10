"""Offscreen palette/scale regression; does not change Windows settings."""

import os
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_SCALE_FACTOR"] = sys.argv[2]

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QColor, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.backup import BackupManager
from officeflow.presentation.attachment_search_page import AttachmentSearchPageWidget
from officeflow.presentation.backup_files_dialog import BackupFilesDialog
from officeflow.presentation.theme import LIGHT_STYLESHEET
from tests.integration.test_attachment_documents import documents
from tests.integration.test_reliability_phase2 import populate


def settle(test):
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: loop.quit() if test() else None)
    timer.start(10)
    QTimer.singleShot(15000, loop.quit)
    loop.exec()
    timer.stop()
    assert test()


output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
app = QApplication([])
for filename in ("malgun.ttf", "malgunbd.ttf", "segoeui.ttf"):
    assert QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / filename)) >= 0
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
with tempfile.TemporaryDirectory(prefix="officeflow-phase2-visual-") as temporary:
    paths = AppPaths(Path(temporary))
    paths.ensure_directories()
    with documents(paths.data_dir, attachment_root=paths.attachment_dir) as (env, creation):
        populate(env, creation)
        manager = BackupManager(paths)
        archive = manager.create_backup()
        dialog = BackupFilesDialog(manager)
        dialog.resize(500, 420)
        dialog.show()
        dialog.load(archive.path)
        settle(lambda: dialog._thread is None)
        dialog.list.item(0).setSelected(True)
        app.processEvents()
        assert dialog.list.count() == 3
        assert dialog.list.height() >= 80
        for button in (
            dialog.save_button,
            dialog.next_button,
            dialog.close_button,
            dialog.search_button,
        ):
            if button.isVisible():
                assert dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight()))
        assert (
            dialog.list.viewport()
            .grab()
            .toImage()
            .pixelColor(2, dialog.list.viewport().height() - 2)
            .lightness()
            > 180
        )
        assert dialog.grab().save(str(output / "backup-files.png"))
        page = AttachmentSearchPageWidget(env.search, "Asia/Seoul")
        page.setStyleSheet(LIGHT_STYLESHEET)
        page.resize(648, 470)
        page.show()
        page.activate("서식")
        page.more_conditions.setChecked(True)
        settle(lambda: page._worker is None and len(page.model.items) == 3)
        page.list_view.setCurrentIndex(page.model.index(0, 0))
        app.processEvents()
        for widget in (page.date_to, page.source_combo, page.open_button, page.save_button):
            assert page.rect().contains(widget.mapTo(page, widget.rect().bottomRight()))
        assert (
            page.list_view.viewport()
            .grab()
            .toImage()
            .pixelColor(2, page.list_view.viewport().height() - 2)
            .lightness()
            > 180
        )
        assert page.grab().save(str(output / "search-materials.png"))
        page.shutdown()
        page.close()
        dialog.reject()
print("phase 2 visual assertions passed")
