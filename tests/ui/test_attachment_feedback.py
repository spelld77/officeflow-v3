"""Inspection results stay readable; opening a document needs no success dismissal."""

from types import SimpleNamespace

import pytest
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QLabel, QPlainTextEdit

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.attachment_export import AttachmentExportService
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
from officeflow.presentation.document_actions import file_actions
from tests.integration.test_document_lifecycle import lifecycle, original
from tests.ui.test_document_lifecycle_ui import records


def make_records(env, item, tmp_path):
    exports = AttachmentExportService(env.attachments, ExternalExportStorage(tmp_path / "journal"))
    return records(env, item, exports)


def test_information_check_remains_visible_after_samples_and_selection(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        source = tmp_path / "other.txt"
        source.write_bytes(b"other")
        other = env.attachments.attach(item.task_id, source)
        dialog = make_records(env, item, tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.select_attachment(item.id)
        dialog.verify_attachment_button.click()
        qtbot.waitUntil(lambda: dialog._verification_thread is None, timeout=10000)
        results = [
            box
            for box in file_actions(dialog, service).dialogs
            if box.objectName() == "attachmentInformationDialog"
        ]
        assert len(results) == 1
        information = results[0]
        text = information.findChild(QPlainTextEdit).toPlainText()
        assert "확인 완료" in text and item.name in text and item.checksum in text
        height = dialog.attachment_list.viewport().height()
        dialog.select_attachment(other.id)
        dialog._schedule_file_samples()
        qtbot.wait(350)
        assert information.isVisible()
        assert information.findChild(QPlainTextEdit).toPlainText() == text
        assert dialog.attachment_list.viewport().height() >= height
        dialog.shutdown()


@pytest.mark.parametrize("kind", ["general", "original", "version"])
def test_document_open_success_closes_progress_without_completion_notice(
    qtbot, tmp_path, monkeypatch, kind
):
    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        if kind == "general":
            path = tmp_path / "normal.txt"
            path.write_bytes(b"normal")
            item = env.attachments.attach(item.task_id, path)
        entry_kind, identity = "attachment", item.id
        if kind == "version":
            work = service.create(
                service.catalog.get("attachment", item.id),
                DocumentOperation.WORK,
                filename="작업본.txt",
            )
            version = service.create(
                service.catalog.get("attachment", work.attachment_id),
                DocumentOperation.VERSION,
                label="검토본",
            )
            entry_kind, identity = "version", version.version_id
        dialog = make_records(env, item, tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        controller = file_actions(dialog, service)
        feedback = QLabel("기존 파일 정보", dialog)
        opened = []
        monkeypatch.setattr(
            QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True
        )
        for _attempt in range(3):
            controller.open(entry_kind, identity, feedback=feedback)
            qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
            assert not controller.dialogs and not controller._retired
            assert feedback.text() == "기존 파일 정보"
        assert len(opened) == 3
        dialog.shutdown()


def test_information_check_error_remains_visible_after_refresh(qtbot, tmp_path, monkeypatch):
    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        dialog = make_records(env, item, tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.select_attachment(item.id)

        def rejected(*_args, **_kwargs):
            raise OSError("보안 프로그램이 파일 읽기를 거절했습니다.")

        monkeypatch.setattr(env.attachments, "verify", rejected)
        dialog.verify_attachment_button.click()
        qtbot.waitUntil(lambda: dialog._verification_thread is None, timeout=10000)
        information = file_actions(dialog, service).dialogs[-1]
        assert "보안 프로그램" in information.findChild(QPlainTextEdit).toPlainText()
        dialog._schedule_file_samples()
        qtbot.wait(350)
        assert information.isVisible()
        assert "보안 프로그램" in information.findChild(QPlainTextEdit).toPlainText()
        dialog.shutdown()


@pytest.mark.parametrize("failure", ["prepare", "launch"])
def test_document_open_failure_keeps_error_visible(qtbot, tmp_path, monkeypatch, failure):
    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        dialog = make_records(env, item, tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        controller = file_actions(dialog, service)
        if failure == "prepare":

            def rejected(*_args):
                raise OSError("파일을 읽을 수 없습니다.")

            monkeypatch.setattr(service, "prepare_open", rejected)
        monkeypatch.setattr(QDesktopServices, "openUrl", lambda _url: False)
        controller.open("attachment", item.id)
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        assert len(controller.dialogs) == 1 and controller.dialogs[0].isVisible()
        assert any(
            "못했습니다" in label.text() or "없습니다" in label.text()
            for label in controller.dialogs[0].findChildren(QLabel)
        )
        dialog.shutdown()


@pytest.mark.parametrize("warning", [False, True])
def test_other_command_success_or_warning_still_needs_confirmation(qtbot, tmp_path, warning):
    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        dialog = make_records(env, item, tmp_path)
        qtbot.addWidget(dialog)
        controller = file_actions(dialog, service)
        controller.run(
            "다른 파일 작업",
            lambda _canceled: (
                SimpleNamespace(warning="일부 내용은 확인이 필요합니다.")
                if warning
                else "저장 완료"
            ),
            close_on_success=warning,
        )
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        assert len(controller.dialogs) == 1 and controller.dialogs[0].isVisible()
        assert any(
            ("확인이 필요" if warning else "저장 완료") in label.text()
            for label in controller.dialogs[0].findChildren(QLabel)
        )
        dialog.shutdown()
