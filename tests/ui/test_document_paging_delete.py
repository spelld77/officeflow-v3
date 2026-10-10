"""Navigation appears only for real extra pages; highlighted selections delete together."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QPushButton

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.attachment_export import AttachmentExportService
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
from officeflow.presentation.document_actions import DocumentBrowser, file_actions
from tests.integration.test_attachment_documents import seed
from tests.integration.test_document_lifecycle import lifecycle
from tests.integration.test_multiple_attachment_delete import pair
from tests.ui.test_attachment_feedback import make_records


def browser(env, facade, task_id, tmp_path):
    exports = AttachmentExportService(env.attachments, ExternalExportStorage(tmp_path / "journal"))
    box = DocumentBrowser(facade, exports, task_id)
    box.kind.setCurrentIndex(1)
    return box


@pytest.mark.parametrize("count", [0, 2, 100, 101])
def test_page_navigation_has_actual_boundaries(qtbot, tmp_path, count):
    with lifecycle(tmp_path) as (env, facade):
        first = seed(env)
        path = tmp_path / "added.txt"
        for number in range(max(0, count - 1)):
            path.write_bytes(f"content {number}".encode())
            env.attachments.attach(first.task_id, path)
        if count == 0:
            env.attachments.delete_file(first.id)
        box = browser(env, facade, first.task_id, tmp_path)
        qtbot.addWidget(box)
        box.show()
        previous = box.findChild(QPushButton, "documentPreviousPageButton")
        following = box.findChild(QPushButton, "documentNextPageButton")
        assert previous is not None and following is not None
        assert previous.isVisible() == following.isVisible() == (count > 100)
        assert box.list.count() == min(count, 100)
        if count > 100:
            assert not previous.isEnabled() and following.isEnabled()
            following.click()
            assert box.list.count() == 1 and previous.isEnabled() and not following.isEnabled()
            box.next()
            assert box.list.count() == 1 and len(box.cursors) == 2
            previous.click()
            assert box.list.count() == 100
        else:
            box.next()
            assert len(box.cursors) == 1 and box.list.count() == count
        box.shutdown()


@pytest.mark.parametrize("screen", ["records", "browser"])
def test_two_selected_files_delete_after_one_confirmation(qtbot, tmp_path, screen):
    with lifecycle(tmp_path) as (env, facade):
        first, _second = pair(env)
        box = (
            make_records(env, first, tmp_path)
            if screen == "records"
            else browser(env, facade, first.task_id, tmp_path)
        )
        qtbot.addWidget(box)
        box.show()
        listing = box.attachment_list if screen == "records" else box.list
        listing.setCurrentRow(0)
        listing.item(1).setSelected(True)
        assert len(listing.selectedItems()) == 2
        if screen == "records":
            menu = box._build_attachment_context_menu()
            action = next(a for a in menu.actions() if "삭제" in a.text())
            assert action.isEnabled()
            action.trigger()
        else:
            box.delete()
        controller = file_actions(box, facade)
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        confirmation = next(
            dialog
            for dialog in controller.dialogs
            if dialog.objectName() == "fileDeleteConfirmation"
        )
        button = confirmation.findChild(QPushButton, "confirmFileDeleteButton")
        assert not button.isEnabled()
        confirmation.findChild(QCheckBox, "fileDeleteConsent").setChecked(True)
        button.click()
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        assert env.attachments.attachment_metadata_for_task(first.task_id) == ()
        assert listing.count() == 0
        box.shutdown()


@pytest.mark.parametrize("count", [50, 51])
def test_version_page_boundary_and_delete_last_page_returns_to_previous(qtbot, tmp_path, count):
    with lifecycle(tmp_path) as (env, facade):
        item = seed(env)
        for number in range(count):
            facade.create(
                facade.catalog.get("attachment", item.id),
                DocumentOperation.VERSION,
                label=f"보관 {number}",
            )
        box = browser(env, facade, item.task_id, tmp_path)
        qtbot.addWidget(box)
        box.kind.setCurrentIndex(0)
        box.show()
        assert box.list.count() == 50 and box.has_next == (count == 51)
        assert box.navigation.isVisible() == (count == 51)
        if count == 51:
            box.list.item(0).setCheckState(Qt.CheckState.Checked)
            checked = set(box.selected)
            box.next()
            assert box.list.count() == 1
            key = box.list.item(0).data(Qt.ItemDataRole.UserRole)
            facade.delete(facade.plan(*key))
            box.refresh()
            assert (
                box.list.count() == 50 and len(box.cursors) == 1 and not box.navigation.isVisible()
            )
            assert box.selected == checked
        box.cursors.append(1)
        box.kind.setCurrentIndex(1)
        assert box.cursors == [None] and not box.navigation.isVisible()
        box.all_families.setChecked(False)
        assert box.cursors == [None]
        box.shutdown()


def test_browser_checked_storage_targets_are_not_implicitly_deleted(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        first, _second = pair(env)
        extra_source = tmp_path / "keep.txt"
        extra_source.write_bytes(b"keep")
        keep = env.attachments.attach(first.task_id, extra_source)
        box = browser(env, facade, first.task_id, tmp_path)
        qtbot.addWidget(box)
        box.show()
        for index in range(box.list.count()):
            row = box.list.item(index)
            if row.data(Qt.ItemDataRole.UserRole) == ("attachment", keep.id):
                row.setCheckState(Qt.CheckState.Checked)
            else:
                row.setSelected(True)
        assert len(box.list.selectedItems()) == 2
        box.delete()
        controller = file_actions(box, facade)
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        confirmation = next(
            d for d in controller.dialogs if d.objectName() == "fileDeleteConfirmation"
        )
        confirmation.findChild(QCheckBox, "fileDeleteConsent").setChecked(True)
        confirmation.findChild(QPushButton, "confirmFileDeleteButton").click()
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        assert [row.id for row in env.attachments.attachment_metadata_for_task(first.task_id)] == [
            keep.id
        ]
        assert ("attachment", keep.id) in box.selected
        box.shutdown()


@pytest.mark.parametrize("screen", ["records", "browser"])
def test_canceling_multiple_delete_keeps_selection_and_files(qtbot, tmp_path, screen):
    with lifecycle(tmp_path) as (env, facade):
        first, second = pair(env)
        box = (
            make_records(env, first, tmp_path)
            if screen == "records"
            else browser(env, facade, first.task_id, tmp_path)
        )
        qtbot.addWidget(box)
        box.show()
        listing = box.attachment_list if screen == "records" else box.list
        listing.setCurrentRow(0)
        listing.item(1).setSelected(True)
        if screen == "records":
            box._delete_attachment()
        else:
            box.delete()
        controller = file_actions(box, facade)
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        confirmation = next(
            d for d in controller.dialogs if d.objectName() == "fileDeleteConfirmation"
        )
        confirmation.reject()
        assert len(listing.selectedItems()) == 2
        assert {row.id for row in env.attachments.attachment_metadata_for_task(first.task_id)} == {
            first.id,
            second.id,
        }
        assert (env.attachments.storage_root / first.relative_path).read_bytes() == b"initial"
        assert (env.attachments.storage_root / second.relative_path).read_bytes() == b"second"
        box.shutdown()


def test_read_only_browser_blocks_multiple_delete(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        first, _second = pair(env)
        exports = AttachmentExportService(
            env.attachments, ExternalExportStorage(tmp_path / "journal")
        )
        box = DocumentBrowser(facade, exports, first.task_id, read_only=True)
        qtbot.addWidget(box)
        box.kind.setCurrentIndex(1)
        box.show()
        box.list.setCurrentRow(0)
        box.list.item(1).setSelected(True)
        assert not next(
            action for action in box._build_menu().actions() if "삭제" in action.text()
        ).isEnabled()
        box.delete()
        controller = file_actions(box, facade)
        assert controller._job_thread is None and not controller.dialogs
        assert len(env.attachments.attachment_metadata_for_task(first.task_id)) == 2
        box.shutdown()
