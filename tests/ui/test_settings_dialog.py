from __future__ import annotations

from pytestqt.qtbot import QtBot

from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.settings_dialog import SettingsDialog


def test_settings_dialog_normalizes_desktop_options(qtbot: QtBot) -> None:
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)
    dialog.shortcut_edit.setText("control + shift + f8")
    dialog.start_with_windows_check.setChecked(True)
    dialog.minimize_to_tray_check.setChecked(False)
    dialog.grace_minutes_spin.setValue(90)
    dialog.automatic_backup_check.setChecked(True)
    dialog.backup_interval_spin.setValue(12)
    dialog.backup_keep_spin.setValue(7)

    assert "프로그램 종료·절전" in dialog.grace_minutes_spin.toolTip()
    assert "범위보다 오래된 알림" in dialog.settings_hint.text()

    dialog._validate_and_accept()
    settings = dialog.settings()

    assert settings.global_quick_add_shortcut == "Ctrl+Shift+F8"
    assert settings.start_with_windows is True
    assert settings.minimize_to_tray is False
    assert settings.missed_reminder_grace_minutes == 90
    assert settings.automatic_backup_enabled is True
    assert settings.automatic_backup_interval_hours == 12
    assert settings.automatic_backup_keep == 7


def test_settings_dialog_keeps_open_for_invalid_shortcut(qtbot: QtBot) -> None:
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)
    dialog.shortcut_edit.setText("O")

    dialog._validate_and_accept()

    assert dialog.error_label.isVisible() is False or dialog.error_label.text()
    assert "보조 키" in dialog.error_label.text()
