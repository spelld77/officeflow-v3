"""Attachment order controls and cross-window interaction."""

from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from officeflow.application.tasks import TaskDraft
from officeflow.domain.attachment import AttachmentMove
from officeflow.presentation.record_dialog import AttachmentImportWorker, TaskRecordsDialog
from officeflow.presentation.theme import LIGHT_STYLESHEET
from tests.integration.test_attachment_search import search_environment


@pytest.fixture
def order_environment(tmp_path, qtbot):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="첨부 순서"))
        items = [env.add(task.id, f"파일{i}.txt") for i in range(6)]
        dialogs = []

        def open_dialog(task_value=task, occurrence_start=None):
            dialog = TaskRecordsDialog(
                task_value,
                task_service=env.tasks,
                record_service=env.records,
                attachment_service=env.attachments,
                occurrence_start=occurrence_start,
                initial_tab="attachments",
            )
            dialog.setStyleSheet(LIGHT_STYLESHEET)
            qtbot.addWidget(dialog)
            dialog.show()
            dialogs.append(dialog)
            return dialog

        yield env, task, items, open_dialog
        for dialog in dialogs:
            dialog.shutdown()
        assert not env.attachments._listeners


def _ids(dialog):
    return [
        dialog.attachment_list.item(i).data(Qt.ItemDataRole.UserRole)
        for i in range(dialog.attachment_list.count())
    ]


def _order_action(dialog, label):
    return next(
        action
        for action in dialog._build_attachment_context_menu().actions()
        if action.text() == label
    )


@pytest.mark.parametrize("view_mode", ["list", "thumbnails"])
def test_context_menu_order_without_toolbar_buttons(order_environment, qtbot, view_mode):
    env, task, items, open_dialog = order_environment
    dialog = open_dialog()
    dialog._set_attachment_view(view_mode)
    tab = dialog.tabs.currentWidget()
    assert not any(button.text() in ("위로", "아래로") for button in tab.findChildren(QPushButton))
    changes = []
    dialog.changed.connect(lambda: changes.append(True))
    assert not _order_action(dialog, "위로").isEnabled()
    dialog.select_attachment(items[0].id)
    assert not _order_action(dialog, "위로").isEnabled()
    assert _order_action(dialog, "아래로").isEnabled()
    dialog.select_attachment(items[3].id)
    dialog.activateWindow()
    qtbot.wait(20)
    dialog.attachment_list.setFocus()
    _order_action(dialog, "위로").trigger()
    assert _ids(dialog) == [items[i].id for i in (0, 1, 3, 2, 4, 5)]
    assert dialog._selected_attachment_id == items[3].id
    assert dialog.attachment_list.hasFocus()
    _order_action(dialog, "아래로").trigger()
    assert _ids(dialog) == [item.id for item in items]
    assert dialog._selected_attachment_id == items[3].id
    _order_action(dialog, "맨 아래로").trigger()
    assert _ids(dialog)[-1] == items[3].id
    assert not _order_action(dialog, "아래로").isEnabled()
    _order_action(dialog, "맨 위로").trigger()
    assert _ids(dialog)[0] == items[3].id
    assert not _order_action(dialog, "위로").isEnabled()
    qtbot.wait(10)
    assert changes == []
    assert env.attachments.attachment_metadata_for_task(task.id)[0].id == items[3].id


def test_two_owners_and_repeat_windows_sync_without_losing_input(order_environment, qtbot):
    env, task, items, open_dialog = order_environment
    first = open_dialog()
    second = open_dialog()
    third = open_dialog(occurrence_start=datetime(2026, 10, 6, tzinfo=UTC))
    for dialog in (first, second, third):
        dialog.select_attachment(items[2].id)
        dialog.result_edit.setPlainText("미저장 결과")
        dialog.log_content_edit.setPlainText("미저장 업무일지")
        dialog.checklist_edit.setText("미저장 체크리스트")
    first._move_attachment(AttachmentMove.FIRST)
    qtbot.waitUntil(lambda: all(_ids(dialog)[0] == items[2].id for dialog in (second, third)))
    for dialog in (first, second, third):
        assert dialog._selected_attachment_id == items[2].id
        assert dialog.result_edit.toPlainText() == "미저장 결과"
        assert dialog.log_content_edit.toPlainText() == "미저장 업무일지"
        assert dialog.checklist_edit.text() == "미저장 체크리스트"
    assert env.repository.list_attachments(task.id)[0].id == items[2].id


def test_busy_window_defers_sync_and_moves_disabled(order_environment, qtbot):
    env, task, items, open_dialog = order_environment
    dialog = open_dialog()
    dialog.select_attachment(items[1].id)
    dialog._attachment_thread = object()
    dialog._set_attachment_actions_enabled(False)
    before = _ids(dialog)
    assert not _order_action(dialog, "위로").isEnabled()
    env.attachments.move_attachment(task.id, items[-1].id, AttachmentMove.FIRST)
    qtbot.waitUntil(lambda: dialog._attachment_refresh_pending)
    assert _ids(dialog) == before
    dialog._move_attachment(AttachmentMove.FIRST)
    assert env.repository.list_attachments(task.id)[0].id == items[-1].id
    dialog._attachment_thread = None
    dialog._refresh_pending_attachments()
    assert _ids(dialog)[0] == items[-1].id
    assert dialog._selected_attachment_id == items[1].id
    dialog._verification_thread = object()
    dialog._set_attachment_actions_enabled(True)
    assert not _order_action(dialog, "위로").isEnabled()
    dialog._verification_thread = None


def test_trash_and_stale_task_disallow_reordering(order_environment):
    env, task, items, open_dialog = order_environment
    stale = open_dialog()
    stale.select_attachment(items[1].id)
    trashed = env.tasks.move_to_trash(task.id)
    before = env.repository.list_attachments(task.id)
    stale._move_attachment(AttachmentMove.FIRST)
    assert env.repository.list_attachments(task.id) == before
    dialog = open_dialog(trashed)
    dialog.select_attachment(items[1].id)
    assert not _order_action(dialog, "위로").isEnabled()
    assert not _order_action(dialog, "아래로").isEnabled()


def test_batch_append_partial_failure_and_duplicate_keep_success_order(
    order_environment, tmp_path, qtbot
):
    env, task, original, open_dialog = order_environment
    dialog = open_dialog()
    first = tmp_path / "새파일A.txt"
    last = tmp_path / "새파일B.txt"
    first.write_text("unique a")
    last.write_text("unique b")
    worker = AttachmentImportWorker(
        env.attachments, task.id, (first, tmp_path / "missing", first, last)
    )
    success, failure = [], []
    worker.succeeded.connect(success.append)
    worker.failed.connect(lambda source, error: failure.append((source, error)))
    worker.run()
    qtbot.waitUntil(lambda: dialog.attachment_list.count() == 8)
    assert _ids(dialog) == [item.id for item in original] + [item.id for item in success]
    assert [item.original_name for item in success] == [first.name, last.name]
    assert len(failure) == 2


@pytest.mark.parametrize("size", [(500, 540), (620, 650)])
def test_small_attachment_toolbar_fits_and_repeat_notice_is_clear(
    order_environment, qtbot, tmp_path, size
):
    env, _task, items, open_dialog = order_environment
    start = datetime(2026, 10, 6, tzinfo=UTC)
    repeated = env.tasks.create(
        TaskDraft(
            title="반복",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            recurrence_rule="FREQ=DAILY",
        )
    )
    for item in items:
        env.add(repeated.id, item.original_name)
    dialog = open_dialog(repeated, start)
    dialog.resize(*size)
    qtbot.wait(30)
    tab = dialog.tabs.currentWidget()
    for button in (
        dialog.add_attachment_button,
        dialog.clipboard_image_button,
        dialog.list_view_button,
        dialog.thumbnail_view_button,
    ):
        assert button.isVisible()
        assert button.width() >= button.sizeHint().width()
        assert tab.rect().contains(button.geometry())
    assert any("모든 반복 회차에서 공유" in label.text() for label in tab.findChildren(QLabel))
    dialog.grab().save(str(tmp_path / "attachment-order-layout.png"))
