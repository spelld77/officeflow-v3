from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from officeflow.application.attachments import (
    AttachmentCanceledError,
    AttachmentIntegrity,
    AttachmentOperationError,
    AttachmentRepository,
    AttachmentService,
    DuplicateAttachmentError,
)
from officeflow.application.tasks import TaskDraft, TaskService
from officeflow.domain.attachment import Attachment, AttachmentValidationError
from officeflow.infrastructure.attachments.storage import ManagedAttachmentStorage
from tests.unit.test_task_service import InMemoryTaskRepository


class InMemoryAttachmentRepository(AttachmentRepository):
    def __init__(self) -> None:
        self.items: dict[int, Attachment] = {}
        self.next_id = 1
        self.fail_add = False
        self.fail_delete = False

    def list_attachments(self, task_id: int) -> tuple[Attachment, ...]:
        return tuple(
            item
            for item in self.items.values()
            if item.task_id == task_id and item.detached_at is None
        )

    def get_attachment(self, attachment_id: int) -> Attachment | None:
        return self.items.get(attachment_id)

    def list_detached(self) -> tuple[Attachment, ...]:
        return tuple(item for item in self.items.values() if item.detached_at is not None)

    def find_active_by_checksum(self, task_id: int, checksum: str) -> Attachment | None:
        return next(
            (
                item
                for item in self.items.values()
                if item.task_id == task_id
                and item.checksum == checksum
                and item.detached_at is None
            ),
            None,
        )

    def add_attachment(self, attachment: Attachment) -> Attachment:
        if self.fail_add:
            raise RuntimeError("database unavailable")
        saved = replace(attachment, id=self.next_id)
        self.items[self.next_id] = saved
        self.next_id += 1
        return saved

    def set_missing_at(
        self,
        attachment_id: int,
        missing_at: datetime | None,
    ) -> Attachment:
        saved = replace(self.items[attachment_id], missing_at=missing_at)
        self.items[attachment_id] = saved
        return saved

    def set_checksum(self, attachment_id: int, checksum: str) -> Attachment:
        saved = replace(self.items[attachment_id], checksum=checksum)
        self.items[attachment_id] = saved
        return saved

    def set_detached_at(
        self, attachment_id: int, detached_at: datetime | None
    ) -> Attachment:
        saved = replace(self.items[attachment_id], detached_at=detached_at)
        self.items[attachment_id] = saved
        return saved

    def delete_attachment(self, attachment_id: int) -> None:
        if self.fail_delete:
            raise RuntimeError("database unavailable")
        del self.items[attachment_id]


def make_attachment_service(
    tmp_path: Path,
) -> tuple[TaskService, AttachmentService, InMemoryAttachmentRepository, ManagedAttachmentStorage]:
    task_service = TaskService(InMemoryTaskRepository())
    repository = InMemoryAttachmentRepository()
    storage = ManagedAttachmentStorage(tmp_path / "attachments")
    service = AttachmentService(repository, storage, task_service)
    return task_service, service, repository, storage


def test_storage_copies_to_uuid_path_and_calculates_checksum(tmp_path: Path) -> None:
    source = tmp_path / "분기 보고서.PDF"
    content = b"officeflow attachment"
    source.write_bytes(content)
    storage = ManagedAttachmentStorage(tmp_path / "managed")

    stored = storage.import_file(source)

    assert stored.stored_name != source.name
    assert stored.stored_name.endswith(".pdf")
    assert stored.size_bytes == len(content)
    assert stored.checksum == hashlib.sha256(content).hexdigest()
    assert storage.resolve(stored.relative_path).read_bytes() == content


def test_storage_rejects_path_escape_and_cleans_canceled_copy(tmp_path: Path) -> None:
    source = tmp_path / "large.bin"
    source.write_bytes(b"x" * (2 * 1024 * 1024))
    storage = ManagedAttachmentStorage(tmp_path / "managed")

    with pytest.raises(AttachmentOperationError):
        storage.resolve("../outside.txt", require_exists=False)
    with pytest.raises(AttachmentCanceledError):
        storage.import_file(source, cancel_requested=lambda: True)

    assert not [path for path in storage.root.rglob("*") if path.is_file()]


def test_attachment_service_marks_missing_and_detects_modified_file(tmp_path: Path) -> None:
    task_service, service, _repository, storage = make_attachment_service(tmp_path)
    task = task_service.create(TaskDraft(title="자료 검토"))
    assert task.id is not None
    source = tmp_path / "자료.txt"
    source.write_text("원본", encoding="utf-8")
    attachment = service.attach(task.id, source)
    assert attachment.id is not None

    storage.resolve(attachment.relative_path).write_text("변경", encoding="utf-8")
    verification = service.verify(attachment.id)
    assert verification.integrity is AttachmentIntegrity.MODIFIED

    storage.remove(attachment.relative_path)
    missing = service.attachments_for_task(
        task.id,
        now=datetime(2026, 9, 15, tzinfo=UTC),
    )
    assert missing[0].missing_at == datetime(2026, 9, 15, tzinfo=UTC)
    assert service.verify(attachment.id).integrity is AttachmentIntegrity.MISSING


def test_database_add_failure_leaves_no_managed_file(tmp_path: Path) -> None:
    task_service, service, repository, storage = make_attachment_service(tmp_path)
    task = task_service.create(TaskDraft(title="실패 복구"))
    assert task.id is not None
    source = tmp_path / "자료.txt"
    source.write_text("data", encoding="utf-8")
    repository.fail_add = True

    with pytest.raises(RuntimeError, match="database unavailable"):
        service.attach(task.id, source)

    assert not [path for path in storage.root.rglob("*") if path.is_file()]


def test_database_delete_failure_restores_quarantined_file(tmp_path: Path) -> None:
    task_service, service, repository, storage = make_attachment_service(tmp_path)
    task = task_service.create(TaskDraft(title="삭제 복구"))
    assert task.id is not None
    source = tmp_path / "자료.txt"
    source.write_text("keep me", encoding="utf-8")
    attachment = service.attach(task.id, source)
    assert attachment.id is not None
    repository.fail_delete = True

    with pytest.raises(RuntimeError, match="database unavailable"):
        service.delete_file(attachment.id)

    assert repository.get_attachment(attachment.id) is not None
    assert storage.resolve(attachment.relative_path).read_text(encoding="utf-8") == "keep me"


def test_unlink_can_be_restored_and_delete_file_removes_it(tmp_path: Path) -> None:
    task_service, service, repository, storage = make_attachment_service(tmp_path)
    task = task_service.create(TaskDraft(title="삭제 구분"))
    assert task.id is not None
    source = tmp_path / "자료.txt"
    source.write_text("first", encoding="utf-8")
    first = service.attach(task.id, source)
    assert first.id is not None

    detached_at = datetime(2026, 9, 19, tzinfo=UTC)
    retained = service.unlink(first.id, now=detached_at)
    assert retained.exists()
    assert repository.get_attachment(first.id).detached_at == detached_at
    assert service.attachments_for_task(task.id) == ()
    assert service.detached_attachments()[0].id == first.id

    service.restore(first.id)
    assert service.attachments_for_task(task.id)[0].id == first.id

    service.unlink(first.id, now=detached_at)
    service.delete_file(first.id)
    assert not retained.exists()
    assert repository.get_attachment(first.id) is None

    source.write_text("second", encoding="utf-8")
    second = service.attach(task.id, source)
    assert second.id is not None
    managed_path = storage.resolve(second.relative_path)
    service.delete_file(second.id)
    assert not managed_path.exists()
    assert repository.get_attachment(second.id) is None


def test_duplicate_file_is_not_copied_twice_for_same_task(tmp_path: Path) -> None:
    task_service, service, repository, storage = make_attachment_service(tmp_path)
    task = task_service.create(TaskDraft(title="중복 방지"))
    assert task.id is not None
    source = tmp_path / "자료.txt"
    source.write_text("same content", encoding="utf-8")
    service.attach(task.id, source)

    with pytest.raises(DuplicateAttachmentError, match="이미 이 업무에 첨부"):
        service.attach(task.id, source)

    assert len(repository.items) == 1
    assert len([path for path in storage.root.rglob("*") if path.is_file()]) == 1


def test_attachment_metadata_rejects_unsafe_relative_path() -> None:
    with pytest.raises(AttachmentValidationError):
        Attachment(
            id=None,
            task_id=1,
            original_name="report.txt",
            stored_name="stored.txt",
            relative_path="../stored.txt",
            size_bytes=1,
            checksum="0" * 64,
            created_at=datetime.now(UTC),
        )
