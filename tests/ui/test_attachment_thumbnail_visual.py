import os
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPalette

from officeflow.application.tasks import TaskDraft
from tests.integration.test_attachment_search import search_environment
from tests.ui.test_attachment_thumbnails import attach_images
from tests.ui.test_image_attachments import make_records


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("size", [(500, 540), (620, 650)])
def test_thumbnail_narrow_and_default_light_style(qtbot, qapp, tmp_path, dark, size):
    palette, font = QPalette(qapp.palette()), QFont(qapp.font())
    try:
        if os.environ.get("OFFICEFLOW_RENDER_FONTS") == "1":
            for name in ("malgun.ttf", "malgunbd.ttf", "segoeui.ttf"):
                assert QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / name)) >= 0
            qapp.setFont(QFont("Malgun Gothic"))
        if dark:
            changed = QPalette(palette)
            for role in (
                QPalette.ColorRole.Window,
                QPalette.ColorRole.Base,
                QPalette.ColorRole.Button,
            ):
                changed.setColor(role, QColor("#202020"))
            for role in (
                QPalette.ColorRole.WindowText,
                QPalette.ColorRole.Text,
                QPalette.ColorRole.ButtonText,
            ):
                changed.setColor(role, QColor("#eeeeee"))
            qapp.setPalette(changed)
        with search_environment(tmp_path) as env:
            task = env.tasks.create(TaskDraft(title="첨부 사진과 업무 문서 확인"))
            items = attach_images(env, task, tmp_path, 7)
            for shape in ((80, 500), (500, 80)):
                source = tmp_path / f"transparent-{shape[0]}.png"
                picture = QImage(*shape, QImage.Format.Format_ARGB32)
                picture.fill(QColor(70, 130, 180, 80))
                assert picture.save(str(source))
                env.attachments.attach(task.id, source)
            for suffix in ("docx", "xlsx", "hwp", "pdf"):
                source = tmp_path / f"문서.{suffix}"
                source.write_bytes(suffix.encode())
                env.attachments.attach(task.id, source)
            env.attachments.rename_attachment(
                items[0].id,
                "매우 긴 사진 이름_" * 10 + ".png",
                expected_revision=items[0].metadata_revision,
            )
            records = make_records(env, task, qtbot)
            try:
                records.resize(*size)
                records.thumbnail_view_button.click()
                records.select_attachment(items[0].id)
                qtbot.waitUntil(lambda: records._image_coordinator.cache_bytes > 0)
                qtbot.wait(150)
                assert records.width() == size[0]
                assert records.attachment_list.verticalScrollBar().isVisible()
                assert records.attachment_list.horizontalScrollBar().maximum() == 0
                assert records.attachment_list.height() >= 180
                for button in (
                    records.list_view_button,
                    records.thumbnail_view_button,
                    records.add_attachment_button,
                    records.clipboard_image_button,
                    records.save_attachment_button,
                    records.more_attachment_button,
                ):
                    assert records.rect().contains(
                        QRect(button.mapTo(records, button.rect().topLeft()), button.size())
                    )
                    assert button.width() >= button.sizeHint().width()
                assert (
                    records.attachment_list.palette().color(QPalette.ColorRole.Base).lightness()
                    > 150
                )
                assert (
                    records.attachment_list.palette().color(QPalette.ColorRole.Text).lightness()
                    < 100
                )
                capture = Path("artifacts/attachment-thumbnail-captures")
                capture.mkdir(parents=True, exist_ok=True)
                suffix = f"{'dark' if dark else 'light'}-{size[0]}-{os.environ.get('QT_SCALE_FACTOR', '1')}"
                assert records.grab().save(str(capture / f"thumbnails-{suffix}.png"))
                records.attachment_list.scrollToBottom()
                qtbot.wait(130)
                assert records.grab().save(str(capture / f"documents-{suffix}.png"))
                records.attachment_list.selectAll()
                assert (
                    len(records.attachment_list.selectedItems()) == records.attachment_list.count()
                )
                assert records._build_attachment_context_menu().actions()[0].text() == "열기"
                assert (
                    len(records.attachment_list.selectedItems()) == records.attachment_list.count()
                )
            finally:
                records.shutdown()
    finally:
        qapp.setPalette(palette)
        qapp.setFont(font)


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows Qt lifetime and memory")
def test_native_thumbnail_repeated_selection_decode_and_shutdown(tmp_path):
    project = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment.update(QT_QPA_PLATFORM="windows", PYTHONUTF8="1", PYTHONPATH=str(project))
    result = subprocess.run(
        [sys.executable, "-m", "tests.ui.attachment_thumbnail_child", str(tmp_path / "profile")],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "NATIVE_THUMBNAILS_OK" in result.stdout
