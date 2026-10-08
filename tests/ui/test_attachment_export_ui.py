from threading import Event
from time import sleep

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QFileDialog, QMenu, QPushButton

from officeflow.application.attachment_export import (
    ExportBatchResult,
    ExportItemResult,
    ExportStatus,
    FileReference,
)
from officeflow.application.records import RecordService
from officeflow.presentation.attachment_recovery import AttachmentRecoveryDialog
from officeflow.presentation.record_dialog import TaskRecordsDialog
from tests.integration.test_attachment_export import attach, files  # noqa: F401
from tests.unit.test_record_service import InMemoryRecordRepository


@pytest.fixture
def dialog(files, tmp_path, qtbot):  # noqa: F811
    tasks, task, attachments, _repo, _managed, _storage, exports, folder = files
    for index in range(4):
        attach(files, tmp_path, name=f"보고서_{index}.txt", content=f"content {index}".encode())
    widget = TaskRecordsDialog(
        task,
        task_service=tasks,
        record_service=RecordService(InMemoryRecordRepository(), tasks),
        attachment_service=attachments,
        attachment_export_service=exports,
        attachment_export_directory=str(folder),
        initial_tab="attachments",
    )
    qtbot.addWidget(widget)
    widget.show()
    yield widget
    widget.shutdown()


def test_ctrl_shift_context_and_refresh_preserve_multiple_selection(dialog, qtbot):
    listing = dialog.attachment_list

    def click(row, modifiers=Qt.KeyboardModifier.NoModifier):
        qtbot.mouseClick(
            listing.viewport(),
            Qt.MouseButton.LeftButton,
            modifiers,
            pos=listing.visualItemRect(listing.item(row)).center(),
        )

    click(0)
    click(2, Qt.KeyboardModifier.ControlModifier)
    assert len(listing.selectedItems()) == 2
    selected = {item.data(Qt.ItemDataRole.UserRole) for item in listing.selectedItems()}
    dialog._refresh_attachments()
    assert {item.data(Qt.ItemDataRole.UserRole) for item in listing.selectedItems()} == selected
    menu = dialog._build_attachment_context_menu()
    enabled_actions = [action.text() for action in menu.actions() if action.isEnabled()]
    menu.deleteLater()
    QTimer.singleShot(50, lambda: [menu.close() for menu in dialog.findChildren(QMenu)])
    dialog._attachment_context_menu(listing.visualItemRect(listing.item(2)).center())
    assert len(listing.selectedItems()) == 2
    assert enabled_actions == ["선택 2개 저장…"]
    assert not dialog.delete_attachment_button.isEnabled()
    QTimer.singleShot(50, lambda: [menu.close() for menu in dialog.findChildren(QMenu)])
    dialog._attachment_context_menu(listing.visualItemRect(listing.item(1)).center())
    assert len(listing.selectedItems()) == 1
    click(3, Qt.KeyboardModifier.ShiftModifier)
    assert len(listing.selectedItems()) == 3


def test_standard_single_and_multiple_picker_defaults(dialog, files, monkeypatch):  # noqa: F811
    *_, folder = files
    listing = dialog.attachment_list
    listing.setCurrentRow(0)
    picker_calls, starts = [], []

    def save(*args, **kwargs):
        picker_calls.append(args)
        return str(folder / "renamed.txt"), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", save)
    controller = dialog._export_controller
    monkeypatch.setattr(
        controller, "start", lambda refs, target, *, single: starts.append((refs, target, single))
    )
    dialog._save_attachment_copy()
    assert picker_calls[0][2].endswith("보고서_0.txt")
    assert starts[-1][2] is True
    listing.item(2).setSelected(True)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args: str(folder))
    dialog._save_attachment_copy()
    assert len(starts[-1][0]) == 2 and starts[-1][1] == folder and starts[-1][2] is False


def test_trash_attachment_can_save_without_mutation(files, tmp_path, qtbot):  # noqa: F811
    attached = attach(files, tmp_path)
    tasks, task, attachments, _repo, _managed, _storage, exports, folder = files
    tasks.move_to_trash(task.id)
    widget = TaskRecordsDialog(
        tasks.get_including_deleted(task.id),
        task_service=tasks,
        record_service=RecordService(InMemoryRecordRepository(), tasks),
        attachment_service=attachments,
        attachment_export_service=exports,
        initial_tab="attachments",
    )
    qtbot.addWidget(widget)
    widget.show()
    widget.select_attachment(attached.id)
    assert widget.save_attachment_button.isEnabled()
    assert widget.open_attachment_button.isEnabled()
    assert not widget.delete_attachment_button.isEnabled()
    assert not widget.unlink_attachment_button.isEnabled()
    result = exports.export(
        (FileReference.attachment(attached.id),), folder / "trash.txt", single=True
    )
    assert result.items[0].status is ExportStatus.SAVED
    assert tasks.get_including_deleted(task.id).deleted_at is not None
    widget.shutdown()


def test_long_save_does_not_block_ui_or_task_state_and_closes_safely(
    dialog, files, qtbot, monkeypatch  # noqa: F811
):
    tasks, task, _attachments, _repo, _managed, storage, _exports, folder = files
    entered = Event()
    original = storage.prepare

    def slow(*args, cancel_requested, **kwargs):
        entered.set()
        while not cancel_requested():
            sleep(0.01)
        return original(*args, cancel_requested=cancel_requested, **kwargs)

    monkeypatch.setattr(storage, "prepare", slow)
    dialog.attachment_list.setCurrentRow(0)
    controller = dialog._export_controller
    reference = FileReference.attachment(dialog._selected_attachment_id)
    controller.start((reference,), folder / "test.txt", single=True)
    qtbot.waitUntil(entered.is_set, timeout=5000)
    assert not controller._progress.isModal()
    tasks.complete(task.id)
    assert tasks.get(task.id).status.value == "completed"
    dialog.attachment_list.setCurrentRow(2)
    assert dialog.attachment_list.currentRow() == 2
    cancel_button = next(
        button
        for button in controller._progress.findChildren(QPushButton)
        if button.text() == "취소"
    )
    qtbot.mouseClick(cancel_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not controller.busy, timeout=5000)
    assert not storage.pending_operations()
    assert not (folder / "test.txt").exists()
    controller.shutdown()


def test_result_only_retries_failures_and_readable_narrow_dark_palette(
    dialog, files, qtbot, qapp, tmp_path, monkeypatch  # noqa: F811
):
    *_, storage, _exports, folder = files
    palette = qapp.palette()
    dark = QPalette(palette)
    dark.setColor(QPalette.ColorRole.Window, QColor("#202020"))
    dark.setColor(QPalette.ColorRole.WindowText, QColor("#eeeeee"))
    qapp.setPalette(dark)
    try:
        controller = dialog._export_controller
        ref = FileReference.attachment(
            dialog._selected_attachment_id
            or dialog.attachment_list.item(0).data(Qt.ItemDataRole.UserRole)
        )
        result = ExportBatchResult(
            "fixture",
            (
                ExportItemResult(ref, "저장.txt", ExportStatus.SAVED, folder / "저장.txt"),
                ExportItemResult(FileReference.attachment(999), "실패.txt", ExportStatus.FAILED),
                ExportItemResult(
                    FileReference.attachment(1000), "확인.txt", ExportStatus.NEEDS_REVIEW
                ),
            ),
            folder,
        )
        controller._target, controller._single = folder, False
        controller.show_result(result)
        result_dialog = controller._dialogs[-1]
        result_dialog.resize(420, 300)
        dialog.resize(600, 520)
        dialog.attachment_list.item(0).setSelected(True)
        dialog.attachment_list.item(2).setSelected(True)
        qtbot.wait(100)
        for button in result_dialog.findChildren(QPushButton):
            assert result_dialog.rect().contains(
                button.mapTo(result_dialog, button.rect().bottomRight())
            )
        assert result_dialog.palette().color(QPalette.ColorRole.Window).lightness() > 180
        assert result_dialog.grab().save(str(tmp_path / "export-result.png"))
        assert dialog.grab().save(str(tmp_path / "attachment-multiselect.png"))
        calls = []
        monkeypatch.setattr(controller, "start", lambda refs, *_args, **_kwargs: calls.append(refs))
        retry = next(
            button
            for button in result_dialog.findChildren(QPushButton)
            if button.text() == "미처리 재시도"
        )
        qtbot.mouseClick(retry, Qt.MouseButton.LeftButton)
        assert calls == [(FileReference.attachment(999),)]
        recovery = AttachmentRecoveryDialog(
            controller._service, storage.inspect_recovery(lambda: False), dialog
        )
        qtbot.addWidget(recovery)
        recovery.resize(440, 380)
        recovery.show()
        qtbot.wait(50)
        assert recovery.grab().save(str(tmp_path / "recovery.png"))
        for button in recovery.findChildren(QPushButton):
            assert recovery.rect().contains(button.mapTo(recovery, button.rect().bottomRight()))
        recovery.reject()
    finally:
        qapp.setPalette(palette)
