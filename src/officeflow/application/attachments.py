from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import BinaryIO, Protocol

from officeflow.application.file_operations import FileJobCanceledError, FileOperationCoordinator
from officeflow.application.tasks import TaskService
from officeflow.domain.attachment import (
    Attachment,
    AttachmentCheckStatus,
    AttachmentMove,
    FileObservation,
)


class AttachmentOperationError(RuntimeError):
    """Raised when an attachment file operation cannot be completed safely."""


class AttachmentCanceledError(AttachmentOperationError):
    """Raised when the user cancels a file copy."""


class AttachmentMissingError(AttachmentOperationError):
    """Raised when an attachment's managed file is absent."""


class DuplicateAttachmentError(AttachmentOperationError):
    """Raised when the same file is already attached to the same task."""


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


@dataclass(frozen=True, slots=True)
class AttachmentQuickSample:
    attachment_id: int
    relative_path: str
    size_bytes: int | None
    mtime_ns: int | None
    state: AttachmentCheckStatus | None


class AttachmentRepository(Protocol):
    def update_observation_if_current(self, expected: Attachment, observation: FileObservation | None, status: AttachmentCheckStatus, checked_at: datetime) -> Attachment | None: ...

    def is_path_registered(self, relative_path: str) -> bool: ...

    def paths_for_task(self, task_id: int) -> tuple[str, ...]: ...
    def list_attachments(self, task_id: int) -> tuple[Attachment, ...]: ...

    def get_attachment(self, attachment_id: int) -> Attachment | None: ...

    def list_detached(self) -> tuple[Attachment, ...]: ...

    def find_active_by_checksum(self, task_id: int, checksum: str) -> Attachment | None: ...

    def add_attachment(self, attachment: Attachment) -> Attachment: ...

    def set_missing_at(self, attachment_id: int, missing_at: datetime | None) -> Attachment: ...

    def set_checksum(self, attachment_id: int, checksum: str) -> Attachment: ...

    def set_detached_at(self, attachment_id: int, detached_at: datetime | None) -> Attachment: ...

    def delete_attachment(self, attachment_id: int) -> None: ...

    def move_attachment(self, task_id: int, attachment_id: int, movement: AttachmentMove) -> tuple[Attachment, ...]: ...


class AttachmentStorage(Protocol):
    coordinator: FileOperationCoordinator
    @property
    def root(self) -> Path: ...

    def observe(self, relative_path: str, *, cancel_requested: Callable[[], bool] | None = None, progress: Callable[[int, int], None] | None = None, now: datetime | None = None) -> FileObservation: ...

    def protected_read(self, relative_path: str) -> AbstractContextManager[BinaryIO]: ...
    def operation_lock(self) -> AbstractContextManager[bool]: ...

    def import_file(
        self,
        source: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
        reserve_published: Callable[[str], None] | None = None,
    ) -> StoredAttachment: ...

    def resolve(self, relative_path: str, *, require_exists: bool = True) -> Path: ...

    def exists(self, relative_path: str) -> bool: ...

    def checksum(
        self, relative_path: str, *, cancel_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> str: ...

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
        self._listeners: list[Callable[[int], None]] = []
        self._listener_lock = RLock()
        self.coordinator = storage.coordinator
        task_service.set_file_delete_guard(self._task_delete_guard)

    @contextmanager
    def _task_delete_guard(self, task_id: int) -> Iterator[None]:
        with self.coordinator.gate:
            self.coordinator.assert_not_pinned(self._repository.paths_for_task(task_id))
            yield

    @property
    def storage_root(self) -> Path:
        return self._storage.root

    def get_metadata(self, attachment_id: int) -> Attachment:
        return self._get_required(attachment_id)

    def quick_samples(self, attachments: tuple[Attachment, ...], *, cancel_requested: Callable[[], bool]) -> tuple[AttachmentQuickSample, ...]:
        samples = []
        for attachment in attachments:
            if cancel_requested():
                break
            try:
                stat = self._storage.resolve(attachment.relative_path).stat()
                samples.append(AttachmentQuickSample(attachment.id_required, attachment.relative_path, stat.st_size, stat.st_mtime_ns, None))
            except FileNotFoundError:
                samples.append(AttachmentQuickSample(attachment.id_required, attachment.relative_path, None, None, AttachmentCheckStatus.MISSING))
            except (OSError, AttachmentOperationError):
                samples.append(AttachmentQuickSample(attachment.id_required, attachment.relative_path, None, None, AttachmentCheckStatus.UNAVAILABLE))
        return tuple(samples)

    def is_path_registered(self, relative_path: str) -> bool:
        return self._repository.is_path_registered(relative_path)

    def observe_result(self, expected: Attachment, observation: FileObservation | None, status: AttachmentCheckStatus, checked_at: datetime, *, notify: bool = True) -> Attachment:
        saved = self._repository.update_observation_if_current(expected, observation, status, checked_at)
        if saved is not None:
            if notify:
                self._notify_changed(saved.task_id)
            return saved
        return self._get_required(expected.id_required)

    def record_observation(self, expected: Attachment, observation: FileObservation, *, notify: bool = True) -> Attachment:
        state = AttachmentCheckStatus.NO_BASELINE if expected.checksum is None else AttachmentCheckStatus.UNCHANGED if expected.checksum == observation.checksum else AttachmentCheckStatus.MODIFIED
        return self.observe_result(expected, observation, state, observation.observed_at, notify=notify)

    def subscribe(self, listener: Callable[[int], None]) -> Callable[[], None]:
        """Listeners must marshal worker-thread notifications onto their UI thread."""
        with self._listener_lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._listener_lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _notify_changed(self, task_id: int) -> None:
        with self._listener_lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(task_id)
            except Exception:
                # A closed UI must never turn an already committed mutation into failure.
                logging.getLogger(__name__).exception("첨부 목록 변경 알림을 전달하지 못했습니다.")

    def attachment_metadata_for_task(self, task_id: int) -> tuple[Attachment, ...]:
        self._task_service.get_including_deleted(task_id)
        return self._repository.list_attachments(task_id)

    def move_attachment(self, task_id: int, attachment_id: int, movement: AttachmentMove) -> tuple[Attachment, ...]:
        self._task_service.get(task_id)
        result = self._repository.move_attachment(task_id, attachment_id, AttachmentMove(movement))
        self._notify_changed(task_id)
        return result

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

    def filename_matches_for_tasks(
        self, task_ids: tuple[int, ...], search: str
    ) -> dict[int, tuple[int, str, int]]:
        """Metadata-only matches; do not inspect files while searching."""
        if len(search.strip()) < 2:
            return {}
        matcher = getattr(self._repository, "filename_matches_for_tasks", None)
        if matcher is not None:
            return dict(matcher(task_ids, search))
        terms = search.strip().lower().split()
        result: dict[int, tuple[int, str, int]] = {}
        for task_id in task_ids:
            matches = [
                item
                for item in self._repository.list_attachments(task_id)
                if all(term in item.original_name.lower() for term in terms)
            ]
            if matches:
                result[task_id] = (matches[0].id_required, matches[0].original_name, len(matches))
        return result

    def attach(
        self,
        task_id: int,
        source: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
        now: datetime | None = None,
    ) -> Attachment:
        try:
            with self.coordinator.job(cancel_requested):
                return self._attach(task_id, source, cancel_requested=cancel_requested, now=now)
        except FileJobCanceledError as error:
            raise AttachmentCanceledError(str(error)) from error

    def _attach(self, task_id: int, source: Path, *, cancel_requested: Callable[[], bool] | None, now: datetime | None) -> Attachment:
        self._task_service.get(task_id)
        source_path = source.expanduser()
        pins = ExitStack()
        try:
            stored = self._storage.import_file(source_path, cancel_requested=cancel_requested, reserve_published=lambda path: pins.enter_context(self.coordinator.pin(path)))
        except BaseException:
            pins.close()
            raise
        warning = ""
        candidates_observed = False
        try:
            duplicate: Attachment | None = None
            lease = ExitStack()
            try:
                for candidate in self._repository.list_attachments(task_id):
                    if cancel_requested and cancel_requested():
                        raise AttachmentCanceledError("첨부 등록을 취소했습니다.")
                    try:
                        path = self._storage.resolve(candidate.relative_path)
                        if path.stat().st_size != stored.size_bytes:
                            continue
                        with self.coordinator.gate:
                            candidate_pin = self.coordinator.pin(candidate.relative_path)
                            lease.enter_context(candidate_pin)
                        lease.enter_context(self._storage.protected_read(candidate.relative_path))
                        observation = self._storage.observe(candidate.relative_path, cancel_requested=cancel_requested)
                        self.record_observation(candidate, observation, notify=False)
                        candidates_observed = True
                        if observation.checksum == stored.checksum:
                            duplicate = candidate
                            break
                    except AttachmentCanceledError:
                        raise
                    except (OSError, AttachmentOperationError):
                        warning = "일부 기존 파일의 중복 확인 불가 · 등록은 유지됨"
                    if duplicate is None:
                        lease.close()
                        lease = ExitStack()
                with self._storage.operation_lock():
                    if duplicate is not None:
                        current = self._repository.get_attachment(duplicate.id_required)
                        if current is not None and current.detached_at is None and current.relative_path == duplicate.relative_path:
                            raise DuplicateAttachmentError(f"같은 내용의 파일이 이미 이 업무에 첨부되어 있습니다: {duplicate.original_name}")
            finally:
                lease.close()
            with self._storage.operation_lock():
                if not self._storage.exists(stored.relative_path):
                    raise AttachmentOperationError("첨부 등록 중 저장 파일이 사라졌습니다. 다시 첨부해 주세요.")
                stat = self._storage.resolve(stored.relative_path).stat()
                timestamp = now or datetime.now(UTC)
                attachment = Attachment(
                    id=None,
                    task_id=task_id,
                    original_name=source_path.name,
                    stored_name=stored.stored_name,
                    relative_path=stored.relative_path,
                    size_bytes=stored.size_bytes,
                    checksum=stored.checksum,
                    created_at=now or datetime.now(UTC),
                    observed_size_bytes=stored.size_bytes,
                    observed_checksum=stored.checksum,
                    observed_mtime_ns=stat.st_mtime_ns,
                    observed_at=timestamp,
                    last_check_status=AttachmentCheckStatus.UNCHANGED,
                    last_checked_at=timestamp,
                )
                saved = self._repository.add_attachment(attachment)
        except Exception:
            pins.close()
            try:
                self._storage.remove(stored.relative_path)
            except Exception as cleanup_error:
                raise AttachmentOperationError(
                    "첨부 정보 저장과 임시 파일 정리에 실패했습니다."
                ) from cleanup_error
            raise
        finally:
            pins.close()
            if candidates_observed:
                self._notify_changed(task_id)
        if not candidates_observed:
            self._notify_changed(task_id)
        return replace(saved, operation_warning=warning)

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

    def verify(
        self, attachment_id: int, *, now: datetime | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> AttachmentVerification:
        try:
            with self.coordinator.job(cancel_requested):
                return self._verify(attachment_id, now=now, cancel_requested=cancel_requested, progress=progress)
        except FileJobCanceledError as error:
            raise AttachmentCanceledError(str(error)) from error

    def _verify(self, attachment_id: int, *, now: datetime | None, cancel_requested: Callable[[], bool] | None, progress: Callable[[int, int], None] | None) -> AttachmentVerification:
        if cancel_requested is not None and cancel_requested():
            raise AttachmentCanceledError("무결성 검사를 취소했습니다.")
        attachment = self._get_required(attachment_id)
        try:
            path = self._storage.resolve(attachment.relative_path)
            with self.coordinator.pin(attachment.relative_path):
                observation = self._storage.observe(attachment.relative_path, cancel_requested=cancel_requested, progress=progress, now=now)
            actual_checksum = observation.checksum
        except FileNotFoundError:
            missing = self.observe_result(attachment, None, AttachmentCheckStatus.MISSING, now or datetime.now(UTC))
            return AttachmentVerification(missing, AttachmentIntegrity.MISSING, None)
        except AttachmentCanceledError:
            raise
        except (OSError, AttachmentOperationError) as error:
            state = AttachmentCheckStatus.CHANGING if isinstance(error, AttachmentOperationError) else AttachmentCheckStatus.UNAVAILABLE
            self.observe_result(attachment, None, state, now or datetime.now(UTC))
            raise
        available = self.record_observation(attachment, observation)
        integrity = (
            AttachmentIntegrity.VERIFIED
            if attachment.checksum is None or actual_checksum == attachment.checksum
            else AttachmentIntegrity.MODIFIED
        )
        return AttachmentVerification(available, integrity, path, actual_checksum)

    def detached_attachments(self) -> tuple[Attachment, ...]:
        return self._repository.list_detached()

    def unlink(self, attachment_id: int, *, now: datetime | None = None) -> Path:
        attachment = self._get_required(attachment_id)
        if attachment.detached_at is not None:
            raise AttachmentOperationError("이미 정리 대기 중인 첨부파일입니다.")
        retained_path = self._storage.resolve(attachment.relative_path, require_exists=False)
        self._repository.set_detached_at(attachment_id, now or datetime.now(UTC))
        self._notify_changed(attachment.task_id)
        return retained_path

    def restore(self, attachment_id: int) -> Attachment:
        attachment = self._get_required(attachment_id)
        if attachment.detached_at is None:
            return attachment
        self._task_service.get_including_deleted(attachment.task_id)
        restored = self._repository.set_detached_at(attachment_id, None)
        self._notify_changed(attachment.task_id)
        return restored

    def delete_file(self, attachment_id: int) -> None:
        task_id = self._get_required(attachment_id).task_id
        try:
            with self._storage.operation_lock():
                self._delete_file_locked(attachment_id)
        finally:
            self._notify_changed(task_id)

    def _delete_file_locked(self, attachment_id: int) -> None:
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

    def remove_files_after_task_delete(
        self,
        relative_paths: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Remove files whose attachment rows were deleted with their task.

        Every path is attempted so one inaccessible file does not prevent the
        remaining files from being cleaned up. Failed paths remain detectable
        as orphan files in data management.
        """
        failed: list[str] = []
        for relative_path in relative_paths:
            try:
                self._storage.remove(relative_path)
            except (OSError, AttachmentOperationError):
                failed.append(relative_path)
        return tuple(failed)

    def _get_required(self, attachment_id: int) -> Attachment:
        attachment = self._repository.get_attachment(attachment_id)
        if attachment is None:
            raise LookupError(f"첨부파일 {attachment_id}을(를) 찾을 수 없습니다.")
        return attachment
