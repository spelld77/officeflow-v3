from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from officeflow.application.tasks import TaskService
from officeflow.domain.attachment import Attachment


class AttachmentOperationError(RuntimeError):
    """Raised when an attachment file operation cannot be completed safely."""


class AttachmentCanceledError(AttachmentOperationError):
    """Raised when the user cancels a file copy."""


class AttachmentMissingError(AttachmentOperationError):
    """Raised when an attachment's managed file is absent."""


class AttachmentIntegrity(StrEnum):
    VERIFIED = "verified"
    MODIFIED = "modified"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class StoredAttachment:
    stored_name: str
    relative_path: str
    size_bytes: int
    checksum: str


@dataclass(frozen=True, slots=True)
class QuarantinedAttachment:
    original_relative_path: str
    quarantine_relative_path: str


@dataclass(frozen=True, slots=True)
class AttachmentVerification:
    attachment: Attachment
    integrity: AttachmentIntegrity
    path: Path | None
    actual_checksum: str | None = None


class AttachmentRepository(Protocol):
    def list_attachments(self, task_id: int) -> tuple[Attachment, ...]: ...

    def get_attachment(self, attachment_id: int) -> Attachment | None: ...

    def add_attachment(self, attachment: Attachment) -> Attachment: ...

    def set_missing_at(
        self, attachment_id: int, missing_at: datetime | None
    ) -> Attachment: ...

    def set_checksum(self, attachment_id: int, checksum: str) -> Attachment: ...

    def delete_attachment(self, attachment_id: int) -> None: ...


class AttachmentStorage(Protocol):
    def import_file(
        self,
        source: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> StoredAttachment: ...

    def resolve(self, relative_path: str, *, require_exists: bool = True) -> Path: ...

    def exists(self, relative_path: str) -> bool: ...

    def checksum(self, relative_path: str) -> str: ...

    def remove(self, relative_path: str) -> None: ...

    def quarantine(self, relative_path: str) -> QuarantinedAttachment: ...

    def restore(self, quarantined: QuarantinedAttachment) -> None: ...

    def purge(self, quarantined: QuarantinedAttachment) -> None: ...


class AttachmentService:
    def __init__(
        self,
        repository: AttachmentRepository,
        storage: AttachmentStorage,
        task_service: TaskService,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._task_service = task_service

    def attachments_for_task(
        self, task_id: int, *, now: datetime | None = None
    ) -> tuple[Attachment, ...]:
        self._task_service.get_including_deleted(task_id)
        checked_at = now or datetime.now(UTC)
        refreshed: list[Attachment] = []
        for attachment in self._repository.list_attachments(task_id):
            is_missing = not self._storage.exists(attachment.relative_path)
            if is_missing and attachment.missing_at is None:
                attachment = self._repository.set_missing_at(attachment.id_required, checked_at)
            elif not is_missing and attachment.missing_at is not None:
                attachment = self._repository.set_missing_at(attachment.id_required, None)
            refreshed.append(attachment)
        return tuple(refreshed)

    def attach(
        self,
        task_id: int,
        source: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
        now: datetime | None = None,
    ) -> Attachment:
        self._task_service.get(task_id)
        source_path = source.expanduser()
        stored = self._storage.import_file(
            source_path,
            cancel_requested=cancel_requested,
        )
        try:
            attachment = Attachment(
                id=None,
                task_id=task_id,
                original_name=source_path.name,
                stored_name=stored.stored_name,
                relative_path=stored.relative_path,
                size_bytes=stored.size_bytes,
                checksum=stored.checksum,
                created_at=now or datetime.now(UTC),
            )
            return self._repository.add_attachment(attachment)
        except Exception:
            try:
                self._storage.remove(stored.relative_path)
            except Exception as cleanup_error:
                raise AttachmentOperationError(
                    "첨부 정보 저장과 임시 파일 정리에 실패했습니다."
                ) from cleanup_error
            raise

    def path_for_open(self, attachment_id: int, *, now: datetime | None = None) -> Path:
        attachment = self._get_required(attachment_id)
        try:
            path = self._storage.resolve(attachment.relative_path)
        except FileNotFoundError as error:
            self._repository.set_missing_at(
                attachment_id,
                attachment.missing_at or now or datetime.now(UTC),
            )
            raise AttachmentMissingError(
                f"'{attachment.original_name}' 파일을 찾을 수 없습니다."
            ) from error
        if attachment.missing_at is not None:
            self._repository.set_missing_at(attachment_id, None)
        return path

    def verify(self, attachment_id: int, *, now: datetime | None = None) -> AttachmentVerification:
        attachment = self._get_required(attachment_id)
        try:
            path = self._storage.resolve(attachment.relative_path)
            actual_checksum = self._storage.checksum(attachment.relative_path)
        except FileNotFoundError:
            missing = self._repository.set_missing_at(
                attachment_id,
                attachment.missing_at or now or datetime.now(UTC),
            )
            return AttachmentVerification(missing, AttachmentIntegrity.MISSING, None)
        available = (
            self._repository.set_missing_at(attachment_id, None)
            if attachment.missing_at is not None
            else attachment
        )
        if attachment.checksum is None:
            available = self._repository.set_checksum(attachment_id, actual_checksum)
            return AttachmentVerification(
                available,
                AttachmentIntegrity.VERIFIED,
                path,
                actual_checksum,
            )
        integrity = (
            AttachmentIntegrity.VERIFIED
            if actual_checksum == attachment.checksum
            else AttachmentIntegrity.MODIFIED
        )
        return AttachmentVerification(available, integrity, path, actual_checksum)

    def unlink(self, attachment_id: int) -> Path:
        attachment = self._get_required(attachment_id)
        retained_path = self._storage.resolve(attachment.relative_path, require_exists=False)
        self._repository.delete_attachment(attachment_id)
        return retained_path

    def delete_file(self, attachment_id: int) -> None:
        attachment = self._get_required(attachment_id)
        try:
            quarantined = self._storage.quarantine(attachment.relative_path)
        except FileNotFoundError:
            self._repository.delete_attachment(attachment_id)
            return
        try:
            self._repository.delete_attachment(attachment_id)
        except Exception:
            try:
                self._storage.restore(quarantined)
            except Exception as restore_error:
                raise AttachmentOperationError(
                    "DB 변경 실패 후 격리한 파일을 원래 위치로 복구하지 못했습니다."
                ) from restore_error
            raise
        try:
            self._storage.purge(quarantined)
        except Exception as error:
            raise AttachmentOperationError(
                "첨부 연결은 제거했지만 격리 파일 정리에 실패했습니다."
            ) from error

    def _get_required(self, attachment_id: int) -> Attachment:
        attachment = self._repository.get_attachment(attachment_id)
        if attachment is None:
            raise LookupError(f"첨부파일 {attachment_id}을(를) 찾을 수 없습니다.")
        return attachment
