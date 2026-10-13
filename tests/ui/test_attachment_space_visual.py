"""Native-capable readability and viewport checks with small U23 fixtures."""

import os
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QPlainTextEdit, QPushButton

from officeflow.application.tasks import TaskDraft
from tests.integration.test_document_lifecycle import lifecycle
from tests.ui.test_attachment_feedback import make_records
from tests.ui.test_attachment_space import choose_kind, mixed_files


@pytest.mark.parametrize("dark", [False, True])
def test_attachment_space_readability_and_five_rows(qtbot, qapp, tmp_path, dark):
    palette, font = QPalette(qapp.palette()), QFont(qapp.font())
    directory = (
        Path("artifacts/u23-captures")
        / f"{'dark' if dark else 'light'}-{os.environ.get('QT_SCALE_FACTOR', '1')}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    try:
        if os.environ.get("OFFICEFLOW_RENDER_FONTS") == "1":
            for filename in ("malgun.ttf", "malgunbd.ttf", "segoeui.ttf"):
                assert (
                    QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / filename)) >= 0
                )
            rendered_font = QFont(font)
            rendered_font.setFamily("Malgun Gothic")
            qapp.setFont(rendered_font)
        injected = QPalette(palette)
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            injected.setColor(role, QColor("#202020" if dark else "#F4F7FB"))
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
        ):
            injected.setColor(role, QColor("#F8FAFC" if dark else "#172033"))
        qapp.setPalette(injected)
        with lifecycle(tmp_path) as (env, facade):
            originals, works = mixed_files(env, facade, tmp_path)
            env.tasks.update(
                originals[0].task_id,
                TaskDraft("긴 업무 이름을 확인하면서 여러 원본과 작업본을 함께 사용하는 첨부 목록"),
            )
            dialog = make_records(env, originals[0], tmp_path)
            qtbot.addWidget(dialog)
            dialog.resize(500, 540)
            dialog.show()
            qtbot.wait(80)

            def capture(name, five=False):
                dialog.layout().activate()
                assert dialog.grab().save(str(directory / f"{name}.png"))
                assert dialog.palette().color(QPalette.ColorRole.Window).lightness() > 150
                assert (
                    dialog.attachment_list.viewport()
                    .palette()
                    .color(QPalette.ColorRole.Base)
                    .lightness()
                    > 150
                )
                for button in dialog.findChildren(QPushButton):
                    if (
                        button.window() is not dialog
                        or not button.isVisible()
                        or button.visibleRegion().isEmpty()
                    ):
                        continue
                    assert (
                        button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 8
                    ), (name, button.text(), button.width())
                    assert button.height() >= button.fontMetrics().height() + 8, (
                        name,
                        button.text(),
                    )
                if five:
                    last = dialog.attachment_list.visualItemRect(dialog.attachment_list.item(4))
                    assert last.bottom() < dialog.attachment_list.viewport().height(), (
                        name,
                        last,
                        dialog.attachment_list.viewport().size(),
                    )
                assert (
                    dialog.attachment_scroll.widget().width()
                    <= dialog.attachment_scroll.viewport().width()
                )

            capture("small-list", five=True)
            dialog.select_attachment(works[0].id)
            qtbot.waitUntil(lambda: "출처:" in dialog.attachment_detail.full_text)
            choose_kind(dialog, "work")
            qtbot.waitUntil(lambda: "출처:" in dialog.attachment_detail.full_text)
            capture("work-filter")
            dialog._show_attachment_information()
            information = dialog._source_info_dialog
            edit = information.findChild(QPlainTextEdit)
            assert edit.palette().color(QPalette.ColorRole.Base).lightness() > 150
            assert edit.isReadOnly() and not information.isModal()
            assert "출처:" in edit.toPlainText()
            assert information.grab().save(str(directory / "information.png"))
            information.reject()
            choose_kind(dialog, "all")
            dialog._set_attachment_view("thumbnails")
            qtbot.wait(80)
            capture("thumbnails")
            dialog._set_attachment_view("list")
            dialog.wide_attachment_button.click()
            qtbot.wait(30)
            capture("wide-list", five=True)
            dialog.shutdown()
    finally:
        qapp.setPalette(palette)
        qapp.setFont(font)
