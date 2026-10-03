from datetime import UTC, datetime
from threading import Event, get_ident
from time import sleep

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QPushButton

from officeflow.application.attachments import AttachmentCanceledError, AttachmentOperationError
from officeflow.application.records import RecordService
from officeflow.application.tasks import TaskDraft
from officeflow.presentation.record_dialog import TaskRecordsDialog
from tests.unit.test_attachment_service import make_attachment_service
from tests.unit.test_record_service import InMemoryRecordRepository


def _services(tmp_path):
    tasks, attachments, repo, storage = make_attachment_service(tmp_path)
    task = tasks.create(TaskDraft(title="큰 파일 검사"))
    source = tmp_path / "payload.bin"
    source.write_bytes(b"original" * 300_000)
    attached = attachments.attach(task.id, source)
    return tasks, attachments, repo, storage, task, attached


def test_checksum_can_cancel_without_changing_saved_baseline(tmp_path):
    _tasks, service, repo, _storage, _task, attached = _services(tmp_path)
    canceled = Event()
    with pytest.raises(AttachmentCanceledError):
        service.verify(
            attached.id,
            cancel_requested=canceled.is_set,
            progress=lambda _done, _total: canceled.set(),
        )
    assert repo.get_attachment(attached.id).checksum == attached.checksum


def test_checksum_rejects_file_changed_during_hash(tmp_path):
    _tasks, service, repo, storage, _task, attached = _services(tmp_path)
    changed = False

    def change(_done, _total):
        nonlocal changed
        if not changed:
            changed = True
            storage.resolve(attached.relative_path).write_bytes(b"changed" * 300_000)

    with pytest.raises(AttachmentOperationError, match="검사 중 파일이 변경"):
        service.verify(attached.id, progress=change)
    assert repo.get_attachment(attached.id).checksum == attached.checksum


def test_checksum_rejects_path_replaced_during_hash(tmp_path):
    _tasks, service, _repo, storage, _task, attached = _services(tmp_path)
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"replacement")
    replaced = False

    def change(_done, _total):
        nonlocal replaced
        if not replaced:
            replaced = True
            # Windows open-file sharing may reject replacement; an in-place
            # change is tested above, this verifies name/handle identity when allowed.
            replacement.replace(storage.resolve(attached.relative_path))

    with pytest.raises((AttachmentOperationError, PermissionError)):
        service.verify(attached.id, progress=change)


def test_verification_runs_off_ui_thread_and_cancel_keeps_dialog_usable(
    tmp_path, qtbot, monkeypatch
):
    tasks, service, _repo, _storage, task, attached = _services(tmp_path)
    entered = Event()
    thread_ids = []

    def slow(_attachment_id, *, cancel_requested=None, progress=None, **_kwargs):
        thread_ids.append(get_ident())
        entered.set()
        while not cancel_requested():
            sleep(0.005)
        raise AttachmentCanceledError("무결성 검사를 취소했습니다.")

    monkeypatch.setattr(service, "verify", slow)
    dialog = TaskRecordsDialog(
        task,
        task_service=tasks,
        record_service=RecordService(InMemoryRecordRepository(), tasks),
        attachment_service=service,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.attachment_list.setCurrentRow(0)
    dialog._verify_attachment()
    qtbot.waitUntil(entered.is_set)
    assert thread_ids != [get_ident()]
    assert not dialog._verification_progress.isModal()
    assert dialog._selected_attachment_id == attached.id
    assert not dialog.delete_attachment_button.isEnabled()
    cancel = next(
        button
        for button in dialog._verification_progress.findChildren(QPushButton)
        if button.text() == "취소"
    )
    cancel.click()
    qtbot.waitUntil(lambda: dialog._verification_thread is None)
    assert dialog.isVisible() and "취소" in dialog.attachment_detail.text()
    assert dialog.verify_attachment_button.isEnabled()
    dialog.shutdown()


def test_owner_shutdown_waits_responsively_for_verification(tmp_path, qtbot, monkeypatch):
    tasks, service, _repo, _storage, task, _attached = _services(tmp_path)
    entered = Event()

    def slow(_attachment_id, *, cancel_requested=None, **_kwargs):
        entered.set()
        while not cancel_requested():
            sleep(0.005)
        sleep(0.12)
        raise AttachmentCanceledError("취소됨")

    monkeypatch.setattr(service, "verify", slow)
    dialog = TaskRecordsDialog(
        task,
        task_service=tasks,
        record_service=RecordService(InMemoryRecordRepository(), tasks),
        attachment_service=service,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.attachment_list.setCurrentRow(0)
    dialog._verify_attachment()
    qtbot.waitUntil(entered.is_set)
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(datetime.now(UTC)))
    timer.start()
    dialog.shutdown()
    timer.stop()
    assert len(ticks) >= 3
    assert dialog._verification_thread is None and not dialog.isVisible()


def test_success_keeps_selected_attachment_and_actions(tmp_path, qtbot):
    tasks, service, _repo, _storage, task, attached = _services(tmp_path)
    dialog = TaskRecordsDialog(
        task,
        task_service=tasks,
        record_service=RecordService(InMemoryRecordRepository(), tasks),
        attachment_service=service,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.select_attachment(attached.id)
    dialog._verify_attachment()
    qtbot.waitUntil(lambda: dialog._verification_thread is None)
    assert dialog._selected_attachment_id == attached.id
    assert (
        dialog.verify_attachment_button.isEnabled()
        and "확인 완료" in dialog.attachment_detail.text()
    )
    dialog.shutdown()
