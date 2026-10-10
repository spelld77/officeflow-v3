"""Selection, compact rows, and saving edited original views through actual UI actions."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QCheckBox, QLineEdit, QPlainTextEdit, QPushButton

from officeflow.application.attachment_export import AttachmentExportService
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
from officeflow.presentation.document_actions import ViewCacheDialog, file_actions
from tests.integration.test_document_lifecycle import lifecycle, original
from tests.ui.test_document_lifecycle_ui import records


@pytest.mark.skipif(sys.platform != "win32", reason="Actual Windows Qt backend")
def test_native_multi_selection_and_owner_shutdown_in_isolated_process(tmp_path):
    project = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment.update(QT_QPA_PLATFORM="windows", PYTHONUTF8="1", PYTHONPATH=str(project))
    result = subprocess.run(
        [sys.executable, "-m", "tests.ui.attachment_selection_child", str(tmp_path / "profile")],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "NATIVE_SELECTION_AND_SHUTDOWN_OK" in result.stdout


def test_cache_check_and_closed_confirmation_keep_selected_copy(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        path = service.prepare_open("attachment", item.id)
        path.write_bytes(b"edited")
        dialog = ViewCacheDialog(service)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.list.setCurrentRow(0)
        key = dialog.current().cache_id
        controller = file_actions(dialog, service)
        dialog.check()
        qtbot.waitUntil(lambda: controller._job_thread is None)
        assert dialog.current() is not None and dialog.current().cache_id == key
        dialog.confirm()
        confirmation = next(
            box for box in controller.dialogs if box.objectName() == "viewClosedConfirmation"
        )
        confirmation.findChild(QCheckBox).setChecked(True)
        next(
            button for button in confirmation.findChildren(QPushButton) if button.text() == "확인"
        ).click()
        qtbot.waitUntil(lambda: controller._job_thread is None)
        assert dialog.current() is not None and dialog.current().closed
        dialog.shutdown()


def test_save_changed_original_as_work_without_reselecting_or_changing_original(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        path = service.prepare_open("attachment", item.id)
        path.write_bytes(b"edited original view")
        dialog = ViewCacheDialog(service)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.list.setCurrentRow(0)
        dialog.preserve()
        controller = file_actions(dialog, service)
        editor = controller.dialogs[-1]
        editor.findChild(QLineEdit).setText("수정한 서식.txt")
        closed = editor.findChild(QCheckBox)
        assert closed is not None
        closed.setChecked(True)
        next(
            button for button in editor.findChildren(QPushButton) if button.text() == "저장"
        ).click()
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        work = next(
            file
            for file in env.attachments.attachment_metadata_for_task(item.task_id)
            if file.role.value == "work"
        )
        assert (
            env.attachments.storage_root / work.relative_path
        ).read_bytes() == b"edited original view"
        assert (env.attachments.storage_root / item.relative_path).read_bytes() == b"initial"
        assert not path.exists()
        assert service.prepare_open("attachment", item.id).read_bytes() == b"initial"
        assert service.cache.check_assets((item.asset_id,), lambda: False)[0].state != "modified"
        dialog.shutdown()


def test_long_names_and_memos_keep_rows_compact_and_list_scrollable(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        for number in range(17):
            source = tmp_path / f"file-{number}.txt"
            source.write_bytes(f"file {number}".encode())
            attached = env.attachments.attach(item.task_id, source)
            attached = env.attachments.rename_attachment(
                attached.id,
                f"{number}-" + "긴 업무 서식 이름 " * 10 + ".txt",
                expected_revision=attached.metadata_revision,
            )
            env.attachments.update_memo(
                attached.id,
                "여러 줄\n메모 내용\n" * 80,
                expected_revision=attached.metadata_revision,
            )
        exports = AttachmentExportService(
            env.attachments, ExternalExportStorage(tmp_path / "journal")
        )
        dialog = records(env, item, exports)
        qtbot.addWidget(dialog)
        dialog.resize(600, 540)
        dialog.show()
        dialog.attachment_list.setCurrentRow(1)
        qtbot.waitUntil(lambda: dialog._sample_thread is not None and not dialog._sample_busy)
        listing = dialog.attachment_list
        assert listing.viewport().height() >= 170
        assert listing.verticalScrollBar().maximum() > 0
        assert listing.verticalScrollBar().isVisible()
        assert listing.verticalScrollBar().width() >= 10
        assert listing.geometry().bottom() < dialog.attachment_detail.geometry().top()
        assert "SHA-256" not in dialog.attachment_detail.text()
        assert all("\n" not in listing.item(row).text() for row in range(listing.count()))
        first_thread = dialog._sample_thread
        assert first_thread is not None
        for _number in range(12):
            listing.item(2).setSelected(True)
            qtbot.wait(130)
            assert len(listing.selectedItems()) == 2
            listing.item(2).setSelected(False)
        assert dialog._sample_thread is first_thread
        dialog.shutdown()
        assert dialog._sample_thread is None


def test_record_changed_view_button_saves_work_and_clears_changed_badge(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        service.changed = env.attachments._notify_changed
        path = service.prepare_open("attachment", item.id)
        path.write_bytes(b"saved edits")
        exports = AttachmentExportService(
            env.attachments, ExternalExportStorage(tmp_path / "journal")
        )
        dialog = records(env, item, exports)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.select_attachment(item.id)
        qtbot.waitUntil(dialog.preserve_view_button.isVisible, timeout=10000)
        assert "수정본 있음" in dialog.attachment_list.item(0).text()
        dialog.preserve_view_button.click()
        copies = next(
            box for box in file_actions(dialog, service).dialogs if isinstance(box, ViewCacheDialog)
        )
        assert copies.current() is not None
        copies.preserve()
        controller = file_actions(copies, service)
        editor = controller.dialogs[-1]
        editor.findChild(QCheckBox).setChecked(True)
        next(
            button for button in editor.findChildren(QPushButton) if button.text() == "저장"
        ).click()
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        qtbot.waitUntil(
            lambda: (
                dialog.attachment_list.count() == 2 and not dialog.preserve_view_button.isVisible()
            ),
            timeout=10000,
        )
        assert "수정본 있음" not in dialog.attachment_list.item(0).text()
        assert (env.attachments.storage_root / item.relative_path).read_bytes() == b"initial"
        dialog.shutdown()


def test_information_opens_separately_and_does_not_shrink_attachment_list(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        exports = AttachmentExportService(
            env.attachments, ExternalExportStorage(tmp_path / "journal")
        )
        dialog = records(env, item, exports)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.select_attachment(item.id)
        height = dialog.attachment_list.viewport().height()
        dialog.attachment_info_button.click()
        info = next(
            box
            for box in file_actions(dialog, service).dialogs
            if box.objectName() == "attachmentInformationDialog"
        )
        assert "SHA-256" in info.findChild(QPlainTextEdit).toPlainText()
        assert info.findChild(QPlainTextEdit).isReadOnly()
        assert dialog.attachment_list.viewport().height() == height
        dialog.shutdown()
