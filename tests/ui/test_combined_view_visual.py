"""Small native-capable captures; no real user data or large image fixture."""

import os
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPalette

from officeflow.application.tasks import TaskDraft
from officeflow.presentation.attachment_metadata import AttachmentMetadataDialog
from officeflow.presentation.record_dialog import TaskRecordsDialog
from tests.integration.test_attachment_search import search_environment


@pytest.mark.parametrize("dark", [False, True])
def test_combined_view_small_window_readability(qtbot, qapp, tmp_path, dark):
    palette, font = QPalette(qapp.palette()), QFont(qapp.font())
    output = (
        Path("artifacts/u27-u29-captures")
        / f"{'dark' if dark else 'light'}-{os.environ.get('QT_SCALE_FACTOR', '1')}"
    )
    output.mkdir(parents=True, exist_ok=True)
    try:
        if os.environ.get("OFFICEFLOW_RENDER_FONTS") == "1":
            for filename in ("malgun.ttf", "malgunbd.ttf", "segoeui.ttf"):
                assert (
                    QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / filename)) >= 0
                )
            rendered = QFont(font)
            rendered.setFamily("Malgun Gothic")
            qapp.setFont(rendered)
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
        with search_environment(tmp_path) as env:
            description = (
                "사업 대상 장비를 확인하고 담당자에게 검토 결과를 전달합니다.\n"
                + "긴 설명도 전체 내용을 그대로 확인하고 복사할 수 있습니다. " * 15
            )
            task = env.tasks.create(
                TaskDraft(
                    "장비 점검 자료 확인 · 제목이 길어도 수정 버튼과 설명이 잘리지 않아야 합니다",
                    description=description,
                )
            )
            for index in range(5):
                env.add(task.id, f"점검 자료 {index + 1}.txt")
            picture = QImage(80, 60, QImage.Format.Format_RGB32)
            picture.fill(QColor("#5685BC"))
            image_path = tmp_path / "점검 화면.webp"
            assert picture.save(str(image_path), "WEBP")
            attached = env.attachments.attach(task.id, image_path)
            records = TaskRecordsDialog(
                task,
                task_service=env.tasks,
                record_service=env.records,
                attachment_service=env.attachments,
            )
            qtbot.addWidget(records)
            records.resize(500, 540)
            records.show()
            qtbot.wait(50)
            try:

                def capture(name):
                    records.layout().activate()
                    assert records.grab().save(str(output / f"{name}.png"))
                    assert records.palette().color(QPalette.ColorRole.Window).lightness() > 150
                    for button in (records.edit_task_button, records.description_panel.toggle):
                        assert records.rect().contains(
                            button.mapTo(records, button.rect().center())
                        )

                capture("view-description")
                body = records.description_panel.body
                assert body.isReadOnly() and body.toPlainText() == task.description
                assert body.palette().color(QPalette.ColorRole.Base).lightness() > 150
                assert body.palette().color(QPalette.ColorRole.Text).lightness() < 150
                records.description_panel.toggle.click()
                qtbot.wait(15)
                capture("expanded-description")
                assert records.tabs.height() >= 120
                records.show_entry("attachments")
                qtbot.wait(15)
                capture("attachments")
                assert records.description_panel.collapsed
                assert (
                    records.attachment_list.viewport().height()
                    >= 5 * records.attachment_list.sizeHintForRow(0)
                )
                records.thumbnail_view_button.click()
                records.select_attachment(attached.id)
                records.attachment_list.scrollToItem(records.attachment_list.currentItem())
                records._schedule_thumbnails(recheck=True)
                qtbot.waitUntil(
                    lambda: (
                        records._image_coordinator.image_for(records._thumbnail_client, attached.id)
                        is not None
                    )
                )
                capture("thumbnails")
                editor = AttachmentMetadataDialog(attached)
                qtbot.addWidget(editor)
                editor.resize(340, 250)
                editor.show()
                editor.name_edit.setText("검토 결과.webp")
                qtbot.wait(20)
                assert editor.grab().save(str(output / "metadata.png"))
                assert (
                    editor.final_name.isVisible() and "검토 결과.webp" in editor.final_name.text()
                )
                editor.name_edit.setText("검토 결과 장비 점검 자료_" * 25 + ".webp")
                qtbot.wait(20)
                assert (
                    editor.final_name.height() <= editor.final_name.fontMetrics().lineSpacing() + 6
                )
                assert "…" in editor.final_name.text()
                assert editor.grab().save(str(output / "metadata-long-name.png"))
            finally:
                records.shutdown()
    finally:
        qapp.setPalette(palette)
        qapp.setFont(font)
