"""One confirmation for release and for changed-view discard, with safe defaults."""

from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton

from officeflow.presentation.document_actions import ViewCacheDialog, file_actions
from tests.integration.test_document_lifecycle import lifecycle, original
from tests.ui.test_attachment_feedback import make_records


def test_original_release_menu_confirmation_and_selection(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        owner = make_records(env, item, tmp_path)
        qtbot.addWidget(owner)
        owner.show()
        owner.select_attachment(item.id)
        menu = owner._build_attachment_context_menu()
        action = next(a for a in menu.actions() if a.text() == "원본 지정 해제…")
        assert action.isEnabled()
        owner._read_only = True
        assert not owner._document_action_enabled("release_original")
        owner._read_only = False
        more = owner.more_attachment_button.menu()
        more.aboutToShow.emit()
        assert next(a for a in more.actions() if a.text() == "원본 지정 해제…").isEnabled()
        action.trigger()
        controller = file_actions(owner, facade)
        box = controller.dialogs[-1]
        assert "해제 전" in " ".join(label.text() for label in box.findChildren(QLabel))
        next(b for b in box.findChildren(QPushButton) if b.text() == "원본 지정 해제").click()
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        assert env.attachments.get_metadata(item.id).role.value == "general"
        assert owner._selected_attachment_id == item.id
        menu = owner._build_attachment_context_menu()
        assert not next(a for a in menu.actions() if a.text() == "원본 지정 해제…").isEnabled()
        assert len(facade.catalog.page(item.task_id)) == 1
        owner.shutdown()


def test_discard_change_after_confirmation_is_kept_and_can_be_reconfirmed(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        view = facade.prepare_open("attachment", item.id)
        owner = ViewCacheDialog(facade)
        qtbot.addWidget(owner)
        owner.show()
        controller = file_actions(owner, facade)
        owner.discard()
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        box = next(b for b in controller.dialogs if b.objectName() == "viewDiscardConfirmation")
        view.write_bytes(b"edit after confirmation")
        box.findChild(QCheckBox).setChecked(True)
        box.findChild(QPushButton, "discardViewButton").click()
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        assert view.exists() and "확인 창을 닫고" in owner.feedback.text()
        box.reject()
        for dialog in tuple(controller.dialogs):
            dialog.reject()
        owner.discard()
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        box = next(b for b in controller.dialogs if b.objectName() == "viewDiscardConfirmation")
        box.findChild(QCheckBox).setChecked(True)
        box.findChild(QPushButton, "discardViewButton").click()
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        assert not view.exists()
        owner.shutdown()


def test_changed_view_discard_needs_only_confirmation_close_checkbox(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        view = facade.prepare_open("attachment", item.id)
        view.write_bytes(b"saved changed bytes without prior check")
        owner = ViewCacheDialog(facade)
        qtbot.addWidget(owner)
        owner.show()
        owner.discard()
        controller = file_actions(owner, facade)
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        box = next(b for b in controller.dialogs if b.objectName() == "viewDiscardConfirmation")
        closed = box.findChild(QCheckBox)
        button = box.findChild(QPushButton, "discardViewButton")
        assert not button.isEnabled() and not closed.isChecked()
        closed.setChecked(True)
        button.click()
        qtbot.waitUntil(lambda: controller._job_thread is None, timeout=10000)
        assert not view.exists() and owner.list.count() == 0
        assert facade.prepare_open("attachment", item.id).read_bytes() == b"initial"
        owner.shutdown()
