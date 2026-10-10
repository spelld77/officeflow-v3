import os
from pathlib import Path

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPalette

from officeflow.application.tasks import TaskDraft
from officeflow.presentation.task_editor import TaskEditorDialog
from officeflow.presentation.theme import LIGHT_STYLESHEET
from tests.integration.test_attachment_search import search_environment
from tests.ui.test_image_attachments import clipboard, image, make_records  # noqa: F401


@pytest.mark.parametrize("dark", [False, True])
def test_clipboard_preview_narrow_light_style(qtbot, qapp, clipboard, tmp_path, dark):  # noqa: F811
    original_palette, original_font = QPalette(qapp.palette()), QFont(qapp.font())
    editor = records = None
    try:
        if os.environ.get("OFFICEFLOW_RENDER_FONTS") == "1":
            for name in ("malgun.ttf", "malgunbd.ttf", "segoeui.ttf"):
                assert QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / name)) >= 0
            qapp.setFont(QFont("Malgun Gothic"))
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
        # Synthetic capture only: no real clipboard contents or business files.
        clipboard.value = image("#dfe9ff")
        painter = QPainter(clipboard.value)
        painter.setPen(QColor("#172033"))
        painter.drawText(10, 40, "TEST")
        painter.end()
        directory = Path("artifacts/image-attachment-ui-captures")
        directory.mkdir(parents=True, exist_ok=True)
        suffix = f"{'dark' if dark else 'light'}-{os.environ.get('QT_SCALE_FACTOR', '1')}"
        with search_environment(tmp_path) as env:
            editor = TaskEditorDialog(timezone="Asia/Seoul", attachment_service=env.attachments)
            qtbot.addWidget(editor)
            editor.setStyleSheet(LIGHT_STYLESHEET)
            editor.title_edit.setText("캡처를 함께 등록하는 업무")
            editor.show()
            editor.resize(460, 520)
            editor.image_panel.add_clipboard()
            qtbot.wait(30)
            assert editor.width() == 460
            for widget in (
                editor.image_panel.add_button,
                editor.image_panel.name_edit,
                editor.image_panel.preview_button,
                editor.image_panel.remove_button,
                editor.save_button,
            ):
                assert editor.rect().contains(
                    QRect(widget.mapTo(editor, widget.rect().topLeft()), widget.size())
                )
            assert (
                editor.image_panel.name_edit.palette().color(QPalette.ColorRole.Text).lightness()
                < 100
            )
            assert (
                editor.image_panel.name_edit.palette().color(QPalette.ColorRole.Base).lightness()
                > 150
            )
            assert editor.grab().save(str(directory / f"editor-{suffix}.png"))
            task = env.tasks.create(TaskDraft(title="기존 첨부 목록"))
            records = make_records(env, task, qtbot)
            records.resize(500, 540)
            records.clipboard_image_button.click()
            child = records._clipboard_dialog
            child.resize(460, 240)
            qtbot.wait(30)
            assert records.width() <= 500
            assert child.width() <= 460
            assert child.grab().save(str(directory / f"clipboard-{suffix}.png"))
            child._save()
            qtbot.waitUntil(lambda: records._clipboard_dialog is None)
            records.select_attachment(env.attachments.attachments_for_task(task.id)[0].id)
            height = records.attachment_list.height()
            records.image_expand_action.trigger()
            viewer = records._image_preview
            viewer.resize(400, 300)
            qtbot.waitUntil(lambda: not viewer._image.isNull())
            qtbot.wait(30)
            assert viewer.width() == 400 and viewer.height() == 300
            assert records.attachment_list.height() == height
            for button in (
                records.add_attachment_button,
                records.clipboard_image_button,
                records.thumbnail_view_button,
                records.list_view_button,
            ):
                assert records.rect().contains(
                    QRect(button.mapTo(records, button.rect().topLeft()), button.size())
                )
                assert button.width() >= button.sizeHint().width()
            assert viewer.picture.palette().color(QPalette.ColorRole.WindowText).lightness() < 100
            assert viewer.palette().color(QPalette.ColorRole.Window).lightness() > 150
            assert viewer.grab().save(str(directory / f"preview-{suffix}.png"))
            assert records.grab().save(str(directory / f"records-{suffix}.png"))
    finally:
        if editor is not None:
            editor.reject()
        if records is not None:
            records.shutdown()
        qapp.setPalette(original_palette)
        qapp.setFont(original_font)
