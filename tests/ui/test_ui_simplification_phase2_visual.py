"""Native-capable small/normal and injected dark/light palette checks for U22-8..14."""

import os
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QCheckBox, QPushButton, QScrollArea

from officeflow.application.exporting import ExportService
from officeflow.application.folder_links import FolderLinkService
from officeflow.application.reminder_preview import ReminderPreviewResult
from officeflow.application.tasks import TaskQuery
from officeflow.domain.hourly_notification import HourlySettings
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.backup_files_dialog import BackupFilesDialog
from officeflow.presentation.data_dialog import DataManagementDialog
from officeflow.presentation.folder_links import FolderLinksPanel
from officeflow.presentation.hourly_notification_settings import HourlyNotificationSettingsDialog
from officeflow.presentation.reminder_preview import ReminderPreviewWidget
from officeflow.presentation.settings_dialog import SettingsDialog
from officeflow.presentation.task_archive_dialog import TaskArchiveDialog
from officeflow.presentation.theme import LIGHT_STYLESHEET
from tests.integration.test_reliability_phase2 import one_backup
from tests.ui.test_ui_simplification_phase2 import FolderListService, preview_item


def capture(widget, directory, name, *, list_widget=None):
    widget.layout().activate()
    if list_widget is not None:
        assert list_widget.viewport().height() >= 150, (name, list_widget.height())
    for button in widget.findChildren(QPushButton):
        if not button.isVisible() or button.visibleRegion().isEmpty():
            continue
        assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 8, (
            name,
            button.text(),
            button.width(),
        )
        assert button.height() >= button.fontMetrics().height() + 8, (
            name,
            button.text(),
            button.height(),
        )
        if button.parentWidget() is widget:
            assert widget.rect().contains(
                QRect(button.mapTo(widget, button.rect().topLeft()), button.size())
            )
    assert widget.palette().color(QPalette.ColorRole.Window).lightness() > 150
    assert widget.palette().color(QPalette.ColorRole.WindowText).lightness() < 100
    for area in widget.findChildren(QScrollArea):
        if area.isVisible():
            assert area.viewport().palette().color(QPalette.ColorRole.Window).lightness() > 150
            assert area.widget().width() <= area.viewport().width(), (
                name,
                area.widget().width(),
                area.viewport().width(),
            )
    for check in widget.findChildren(QCheckBox):
        if check.isVisible():
            assert check.width() >= check.fontMetrics().horizontalAdvance(check.text()) + 24, (
                name,
                check.text(),
                check.width(),
            )
    assert widget.grab().save(str(directory / f"{name}.png"))


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("small", [False, True])
def test_phase2_surfaces_readable_with_optional_details_and_safety(
    qtbot, qapp, tmp_path, dark, small
):
    original_palette, original_font = QPalette(qapp.palette()), QFont(qapp.font())
    windows = []
    directory = (
        Path("artifacts/ui-simplification-phase2-captures")
        / f"{'dark' if dark else 'light'}-{'small' if small else 'normal'}-{os.environ.get('QT_SCALE_FACTOR', '1')}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    try:
        if os.environ.get("OFFICEFLOW_RENDER_FONTS") == "1":
            for font in ("malgun.ttf", "malgunbd.ttf", "segoeui.ttf"):
                assert QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / font)) >= 0
            qapp.setFont(QFont("Malgun Gothic", original_font.pointSize()))
        palette = QPalette(original_palette)
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#202020" if dark else "#f4f7fb"))
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
        ):
            palette.setColor(role, QColor("#eeeeee" if dark else "#172033"))
        qapp.setPalette(palette)
        manager, archive = one_backup(tmp_path)
        preview = ReminderPreviewWidget("Asia/Seoul")
        preview.set_result(ReminderPreviewResult((preview_item(status="미확인"),), False))
        preview.set_filter_active(True)
        folders = FolderLinksPanel(cast(FolderLinkService, FolderListService()), 1)
        general = SettingsDialog(AppSettings())
        hourly = HourlyNotificationSettingsDialog(HourlySettings())
        hourly.minute_spin.setValue(59)
        hourly.next_label.setText("현재: 켜짐 · 다음 정기 알림 내일(일) 00:35")
        hourly.today_label.setText("오늘만 바로 적용 · 10월 10일(토)")
        archive_dialog = TaskArchiveDialog((1, 2))
        data = DataManagementDialog(
            export_service=cast(ExportService, object()), backup_manager=manager, query=TaskQuery()
        )
        backup = BackupFilesDialog(manager)
        for widget in (preview, folders, general, hourly, archive_dialog, data, backup):
            qtbot.addWidget(widget)
            windows.append(widget)
            if not widget.styleSheet():
                widget.setStyleSheet(LIGHT_STYLESHEET)
        sizes = (
            ("today", preview, (560, 240), (780, 260), None),
            ("folders", folders, (470, 380), (650, 460), folders.list),
            ("settings", general, (460, 450), (560, 540), None),
            ("hourly", hourly, (430, 420), (560, 550), None),
            ("archive", archive_dialog, (490, 370), (560, 570), None),
            ("data", data, (500, 480), (720, 650), None),
            ("backup", backup, (500, 420), (700, 560), backup.list),
        )
        for name, widget, minimum, normal, listing in sizes:
            widget.resize(*(minimum if small else normal))
            widget.show()
            qtbot.wait(30)
            if widget is data:
                qtbot.waitUntil(lambda: data._thread is None, timeout=10000)
                capture(data, directory, "data-usage")
                data.tabs.setCurrentIndex(data._tools_tab_index)
                qtbot.wait(30)
            if widget is backup:
                backup.load(archive)
                qtbot.waitUntil(lambda: backup._thread is None, timeout=10000)
            capture(widget, directory, name, list_widget=listing)
            widget.hide()
        general.show()
        general.settings_help.toggle_button.click()
        general.findChild(QScrollArea).ensureWidgetVisible(general.settings_help.label)
        qtbot.wait(30)
        capture(general, directory, "settings-help")
        hourly.show()
        hourly.behavior_help.toggle_button.click()
        hourly.findChild(QScrollArea).ensureWidgetVisible(hourly.behavior_help.label)
        qtbot.wait(30)
        capture(hourly, directory, "hourly-help")
        hourly.behavior_help.toggle_button.click()
        hourly.advanced_toggle.click()
        hourly._add_range("23:00", "06:00")
        hourly.findChild(QScrollArea).ensureWidgetVisible(hourly.add_range_button)
        qtbot.wait(30)
        capture(hourly, directory, "hourly-advanced")
        backup.show()
        backup._show_details()
        qtbot.wait(30)
        capture(backup._detail_dialog, directory, "backup-detail")
        folders.show()
        folders._show_path()
        qtbot.wait(30)
        capture(folders._path_dialog, directory, "folder-path")
        folders.more_menu.ensurePolished()
        assert folders.more_menu.palette().color(QPalette.ColorRole.Window).lightness() > 150
    finally:
        for widget in reversed(windows):
            if hasattr(widget, "shutdown"):
                widget.shutdown()
            widget.close()
        qapp.setPalette(original_palette)
        qapp.setFont(original_font)
