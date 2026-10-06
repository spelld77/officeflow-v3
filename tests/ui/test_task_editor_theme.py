from datetime import UTC, datetime

import pytest
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QLabel, QScrollArea

from officeflow.domain.task import Task
from officeflow.presentation.task_editor import TaskEditorDialog
from officeflow.presentation.theme import LIGHT_STYLESHEET


@pytest.mark.parametrize("period", [False, True])
def test_editor_scroll_body_stays_light_with_dark_system_palette(qapp, qtbot, tmp_path, period):
    original = QPalette(qapp.palette())
    dark = QPalette(original)
    for role, color in (
        (QPalette.ColorRole.Window, "#202020"),
        (QPalette.ColorRole.WindowText, "#EEEEEE"),
        (QPalette.ColorRole.Base, "#1E1E1E"),
        (QPalette.ColorRole.Text, "#EEEEEE"),
        (QPalette.ColorRole.Button, "#252525"),
        (QPalette.ColorRole.ButtonText, "#EEEEEE"),
    ):
        dark.setColor(role, QColor(color))
    qapp.setPalette(dark)
    editor = None
    try:
        task = Task.create(
            title="보고서 확인",
            description="설명과 설정 이름이 밝은 배경에서 보여야 합니다.",
            starts_at=datetime(2026, 9, 20, 3, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 24, 3, 0, tzinfo=UTC) if period else None,
        )
        editor = TaskEditorDialog(timezone="Asia/Seoul", task=task)
        editor.setStyleSheet(LIGHT_STYLESHEET)
        qtbot.addWidget(editor)
        editor.show()
        qtbot.wait(80)
        scroll = editor.findChild(QScrollArea)
        body = scroll.widget()
        image = body.grab().toImage()
        # The blank margin paints the actual body background, not a child input.
        coordinate = round(2 * image.devicePixelRatio())
        assert image.pixelColor(coordinate, coordinate).name() == "#f4f6fa"
        assert body.palette().color(QPalette.ColorRole.Window).name() == "#f4f6fa"
        assert scroll.viewport().palette().color(QPalette.ColorRole.Window).name() == "#f4f6fa"
        for label in body.findChildren(QLabel):
            if label.text() in {"제목 *", "설명", "중요도", "상태"}:
                assert label.palette().color(QPalette.ColorRole.WindowText).name() == "#172033"
        assert editor.grab().save(str(tmp_path / "task-editor-dark-system.png"))

        editor._open_reminder_settings()
        settings = editor._reminder_settings
        assert settings.palette().color(QPalette.ColorRole.Window).name() == "#f4f6fa"
        assert settings.list.palette().color(QPalette.ColorRole.Base).name() == "#ffffff"
        settings._open_rule(False)
        assert settings._child.palette().color(QPalette.ColorRole.Window).name() == "#f4f6fa"
        settings.reject()
    finally:
        if editor is not None:
            editor.reject()
        qapp.setPalette(original)
