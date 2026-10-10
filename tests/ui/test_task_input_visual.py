import os
from pathlib import Path

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

from officeflow.presentation.task_editor import TaskEditorDialog
from officeflow.presentation.theme import LIGHT_STYLESHEET
from tests.ui.test_multiple_reminders_ui import live_window  # noqa: F401


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("advanced", [False, True])
def test_input_and_reminder_capture(live_window, qtbot, qapp, dark, advanced):  # noqa: F811
    window, _env, _task, now, _repo = live_window
    original_palette = QPalette(qapp.palette())
    original_font = QFont(qapp.font())
    editor = None
    try:
        if os.environ.get("OFFICEFLOW_RENDER_FONTS") == "1":
            for name in ("malgun.ttf", "malgunbd.ttf", "segoeui.ttf"):
                assert QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / name)) >= 0
            font = QFont(original_font)
            font.setFamily("Malgun Gothic")
            qapp.setFont(font)
        if dark:
            palette = QPalette(original_palette)
            for role in (
                QPalette.ColorRole.Window,
                QPalette.ColorRole.Base,
                QPalette.ColorRole.Button,
            ):
                palette.setColor(role, QColor("#202020"))
            for role in (
                QPalette.ColorRole.WindowText,
                QPalette.ColorRole.Text,
                QPalette.ColorRole.ButtonText,
            ):
                palette.setColor(role, QColor("#eeeeee"))
            qapp.setPalette(palette)
        editor = TaskEditorDialog(timezone="Asia/Seoul")
        editor.setStyleSheet(LIGHT_STYLESHEET)
        qtbot.addWidget(editor)
        editor.title_edit.setText("새 업무 입력 확인")
        editor.advanced_schedule_check.setChecked(advanced)
        editor.end_enabled_check.setChecked(advanced)
        editor.show()
        editor.resize(460, 520)
        editor.activateWindow()
        qtbot.wait(40)
        editor._scroll.ensureWidgetVisible(editor.start_time_input)
        qtbot.wait(20)
        assert editor.width() <= 460
        assert editor.start_time_edit.palette().color(QPalette.ColorRole.Text).lightness() < 100
        assert editor.start_time_edit.palette().color(QPalette.ColorRole.Base).lightness() > 150
        for button in (editor.start_time_input.minus_button, editor.start_time_input.plus_button):
            assert editor.start_time_input.rect().contains(button.geometry())
            assert button.height() >= button.sizeHint().height()
            assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 8
        alerts = window._reminder_service.poll_due(now=now)
        window._present_reminder_alerts(alerts[:1])
        popup = window._reminder_dialog
        popup.resize(420, 340)
        qtbot.wait(20)
        for button in (
            popup.view_task_button,
            popup.complete_button,
            popup.defer_button,
            popup.acknowledge_button,
        ):
            assert popup.rect().contains(
                QRect(button.mapTo(popup, button.rect().topLeft()), button.size())
            )
            assert button.width() >= button.sizeHint().width()
        directory = Path("artifacts/task-input-ui-captures")
        directory.mkdir(parents=True, exist_ok=True)
        suffix = f"{'dark' if dark else 'light'}-{'advanced' if advanced else 'basic'}-{os.environ.get('QT_SCALE_FACTOR', '1')}"
        assert editor.grab().save(str(directory / f"editor-{suffix}.png"))
        assert popup.grab().save(str(directory / f"reminder-{suffix}.png"))
        editor.start_time_edit.setText("2460")
        editor._validate_and_accept()
        assert editor.start_time_edit.property("inputError") is True
        assert editor.start_time_edit.palette().color(QPalette.ColorRole.Base).name() == "#fff4f4"
        assert editor.grab().save(str(directory / f"error-{suffix}.png"))
    finally:
        if editor is not None:
            editor.reject()
        qapp.setPalette(original_palette)
        qapp.setFont(original_font)
