"""Qt lifecycle routes, owner shutdown, and contrast under a dark system palette."""

from threading import Event

import pytest
from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QColor, QDesktopServices, QPalette
from PySide6.QtWidgets import QCheckBox, QDialog, QPushButton

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.attachment_export import AttachmentExportService
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
from officeflow.presentation.document_actions import DocumentBrowser, ViewCacheDialog, file_actions
from officeflow.presentation.record_dialog import TaskRecordsDialog
from tests.integration.test_document_lifecycle import lifecycle, original


def records(env, item, exports):
    return TaskRecordsDialog(
        env.tasks.get(item.task_id),
        task_service=env.tasks,
        record_service=env.records,
        attachment_service=env.attachments,
        attachment_export_service=exports,
        initial_tab="attachments",
    )


def test_record_menu_roles_browser_and_open_never_passes_fixed_file(
    qtbot, qapp, tmp_path, monkeypatch
):
    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        exports = AttachmentExportService(
            env.attachments, ExternalExportStorage(tmp_path / "journal")
        )
        dialog = records(env, item, exports)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.select_attachment(item.id)
        assert "원본" in dialog.attachment_list.item(0).text()
        menu = dialog._build_attachment_context_menu()
        names = [action.text() for action in menu.actions()]
        assert all(
            label in names
            for label in ["새 작업본…", "보관본 남기기…", "원본으로 지정…", "보관함…"]
        )
        assert dialog.findChild(QPushButton, "documentVersionsButton")
        opened = []

        def launch(url):
            assert QThread.currentThread() == qapp.thread()
            opened.append(url.toLocalFile())
            return True

        monkeypatch.setattr(QDesktopServices, "openUrl", launch)
        dialog._open_attachment()
        qtbot.waitUntil(lambda: bool(opened), timeout=10000)
        assert opened[0] != str(env.attachments.storage_root / item.relative_path)
        dialog.shutdown()


@pytest.mark.parametrize("screen", ["versions", "cache", "create", "delete", "release", "discard"])
def test_new_dialogs_light_and_buttons_reachable_at_narrow_size(qtbot, tmp_path, screen):
    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        service.prepare_open("attachment", item.id)
        exports = AttachmentExportService(
            env.attachments, ExternalExportStorage(tmp_path / "journal")
        )
        owner = QDialog()
        qtbot.addWidget(owner)
        controller = file_actions(owner, service)
        if screen == "versions":
            dialog = DocumentBrowser(service, exports, item.task_id, parent=owner)
        elif screen == "cache":
            dialog = ViewCacheDialog(service, owner)
        elif screen in {"create", "release"}:
            controller.create(
                service.catalog.get("attachment", item.id),
                DocumentOperation.WORK
                if screen == "create"
                else DocumentOperation.RELEASE_ORIGINAL,
                lambda: None,
            )
            dialog = controller.dialogs[-1]
        elif screen == "discard":
            cache_dialog = ViewCacheDialog(service, owner)
            qtbot.addWidget(cache_dialog)
            cache_dialog.discard()
            cache_controller = file_actions(cache_dialog, service)
            qtbot.waitUntil(lambda: cache_controller._job_thread is None, timeout=10000)
            dialog = next(
                box
                for box in cache_controller.dialogs
                if box.objectName() == "viewDiscardConfirmation"
            )
        else:
            controller.delete("attachment", item.id, lambda: None)
            qtbot.waitUntil(
                lambda: any(box.findChildren(QCheckBox) for box in controller.dialogs),
                timeout=10000,
            )
            dialog = next(box for box in controller.dialogs if box.findChildren(QCheckBox))
        qtbot.addWidget(dialog)
        dark = QPalette()
        dark.setColor(QPalette.ColorRole.Window, QColor("#202020"))
        dark.setColor(QPalette.ColorRole.WindowText, QColor("#000000"))
        dialog.setPalette(dark)
        dialog.style().unpolish(dialog)
        dialog.style().polish(dialog)
        dialog.resize(600, 450)
        dialog.show()
        qtbot.wait(30)
        assert dialog.palette().color(QPalette.ColorRole.Window).lightness() > 200
        if hasattr(dialog, "list"):
            assert dialog.list.palette().color(QPalette.ColorRole.Base).lightness() > 200
        for button in dialog.findChildren(QPushButton):
            if button.isVisible():
                assert dialog.rect().contains(button.mapTo(dialog, button.rect().center()))
        controller.shutdown()
        if hasattr(dialog, "shutdown"):
            dialog.shutdown()
        dialog.close()


def test_page_selections_persist_across_kind_and_family_filters(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        work = service.create(
            service.catalog.get("attachment", item.id), DocumentOperation.WORK, filename="작성.txt"
        )
        version = service.create(
            service.catalog.get("attachment", work.attachment_id),
            DocumentOperation.VERSION,
            label="검토본",
        )
        exports = AttachmentExportService(
            env.attachments, ExternalExportStorage(tmp_path / "journal")
        )
        dialog = DocumentBrowser(service, exports, item.task_id)
        qtbot.addWidget(dialog)
        dialog.show()
        assert dialog.list.count() == 1
        dialog.list.item(0).setCheckState(Qt.CheckState.Checked)
        dialog.kind.setCurrentIndex(1)
        assert dialog.list.count() == 2
        dialog.list.item(0).setCheckState(Qt.CheckState.Checked)
        assert ("version", version.version_id) in dialog.selected and len(dialog.selected) == 2
        dialog.kind.setCurrentIndex(0)
        assert dialog.list.item(0).checkState() == Qt.CheckState.Checked
        dialog.shutdown()


def test_shutdown_waits_for_document_worker_without_disabling_other_input(qtbot, tmp_path):
    with lifecycle(tmp_path) as (_env, service):
        owner = QDialog()
        qtbot.addWidget(owner)
        owner.show()
        started, stopped = Event(), Event()
        controller = file_actions(owner, service)

        def operation(canceled):
            started.set()
            while not canceled():
                stopped.wait(0.01)
            stopped.set()

        controller.run("검증 작업", operation)
        qtbot.waitUntil(started.is_set, timeout=10000)
        assert owner.isEnabled()
        assert all(
            dialog.windowModality() == Qt.WindowModality.NonModal for dialog in controller.dialogs
        )
        controller.shutdown()
        assert stopped.is_set() and controller._job_thread is None


def test_late_completion_ignores_destroyed_progress_widget(qtbot, tmp_path):
    from shiboken6 import isValid

    with lifecycle(tmp_path) as (_env, service):
        owner = QDialog()
        qtbot.addWidget(owner)
        controller = file_actions(owner, service)
        started, release = Event(), Event()

        def operation(canceled):
            started.set()
            release.wait(5)
            return "확인 완료"

        controller.run("완료 지연", operation)
        qtbot.waitUntil(started.is_set)
        status = controller._status
        status.deleteLater()
        qtbot.waitUntil(lambda: not isValid(status))
        release.set()
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        controller.shutdown()


def test_repeated_progress_close_and_completion_releases_windows(qtbot, tmp_path):
    with lifecycle(tmp_path) as (_env, service):
        owner = QDialog()
        qtbot.addWidget(owner)
        controller = file_actions(owner, service)
        for _n in range(20):
            controller.run("닫음 경쟁", lambda canceled: "완료")
            progress = controller._progress_dialog
            progress.reject()
            qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
            qtbot.wait(1)
        assert not controller.dialogs and not controller._retired
        controller.shutdown()


def test_page_checks_and_source_preview_stay_bounded_with_long_memo(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        work = service.create(
            service.catalog.get("attachment", item.id),
            DocumentOperation.WORK,
            filename="검토 작업본.txt",
        )
        service.create(
            service.catalog.get("attachment", work.attachment_id),
            DocumentOperation.VERSION,
            label="검토 v2",
            memo="긴 메모 " * 300,
        )
        exports = AttachmentExportService(
            env.attachments, ExternalExportStorage(tmp_path / "journal")
        )
        dialog = DocumentBrowser(service, exports, item.task_id)
        qtbot.addWidget(dialog)
        dialog.resize(600, 450)
        dialog.show()
        dialog.list.setCurrentRow(0)
        assert "검토 작업본.txt" in dialog.feedback.text()
        assert len(dialog.feedback.text()) < 400
        dialog.check_page(True)
        assert len(dialog.selected) == 1
        for button in dialog.findChildren(QPushButton):
            assert dialog.rect().contains(button.mapTo(dialog, button.rect().center()))
        dialog.check_page(False)
        assert not dialog.selected
        dialog.shutdown()
