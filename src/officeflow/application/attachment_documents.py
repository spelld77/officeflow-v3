"""Internal document commands. No Qt, SQL, or physical-path deletion here."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from officeflow.application.attachments import AttachmentCanceledError, AttachmentOperationError
from officeflow.application.file_operations import FileJobCanceledError, FileOperationCoordinator
from officeflow.domain.attachment import validate_display_name


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class DocumentOperation(StrEnum):
    ORIGINAL = "designate_original"
    RELEASE_ORIGINAL = "release_original"
    WORK = "create_work"
    VERSION = "create_version"

    @property
    def creates_frozen(self) -> bool:
        return self in {DocumentOperation.ORIGINAL, DocumentOperation.VERSION}


@dataclass(frozen=True, slots=True)
class DocumentCommand:
    job_id: str
    operation: DocumentOperation
    source_kind: str
    source_id: int
    expected_revision: int
    filename: str = ""
    label: str = ""
    memo: str = ""
    cache_payload: str = ""

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[0-9a-f]{32}", self.job_id)
            or not isinstance(self.operation, DocumentOperation)
            or self.source_kind not in {"attachment", "version", "cache", "snapshot_file"}
            or self.source_id <= 0
            or self.expected_revision < 0
        ):
            raise ValueError("올바른 파일 명령 ID와 출처가 필요합니다.")
        if self.operation is DocumentOperation.WORK:
            validate_display_name(self.filename)
            if self.label or self.memo:
                raise ValueError("작업본에는 출처 메모를 자동 상속하지 않습니다.")
        elif self.operation is DocumentOperation.VERSION:
            if (
                self.filename
                or not self.label.strip()
                or len(self.label) > 200
                or len(self.memo) > 2000
            ):
                raise ValueError("보관 이름은 1~200자, 메모는 2,000자 이하여야 합니다.")
        elif self.filename or self.label or self.memo:
            raise ValueError("원본 지정/해제는 기존 이름과 메모를 유지합니다.")
        if (
            self.source_kind in {"version", "snapshot_file"}
            and self.operation is not DocumentOperation.WORK
        ):
            raise ValueError("보관본에서는 새 작업본만 만들 수 있습니다.")
        if (self.source_kind == "cache") != bool(self.cache_payload):
            raise ValueError("보기 사본 출처 정보가 필요합니다.")
        if self.source_kind == "cache" and self.operation is not DocumentOperation.WORK:
            raise ValueError("보기 사본은 작업본으로만 보존할 수 있습니다.")

    @property
    def request_fingerprint(self) -> str:
        return fingerprint(asdict(self))


@dataclass(frozen=True, slots=True)
class DocumentSource:
    kind: str
    id: int
    task_id: int
    family_id: str
    asset_id: str
    relative_path: str
    name: str
    role: str
    revision: int
    state_fingerprint: str
    provenance: str
    fixed_size: int | None = None
    fixed_checksum: str | None = None


@dataclass(frozen=True, slots=True)
class InternalCapture:
    job_id: str
    asset_id: str
    relative_path: str
    stored_name: str
    size: int
    checksum: str
    captured_at: datetime
    identity: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class DocumentResult:
    job_id: str
    asset_id: str
    attachment_id: int | None = None
    version_id: int | None = None
    retained_attachment_id: int | None = None
    already_committed: bool = False
    warning: str = ""
    retained_version_id: int | None = None

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[0-9a-f]{32}", self.job_id)
            or not re.fullmatch(r"[0-9a-f]{32}", self.asset_id)
            or (self.attachment_id is None) == (self.version_id is None)
            or any(
                value is not None and (type(value) is not int or value <= 0)
                for value in (
                    self.attachment_id,
                    self.version_id,
                    self.retained_attachment_id,
                    self.retained_version_id,
                )
            )
        ):
            raise ValueError("완료한 파일 명령의 식별 정보가 올바르지 않습니다.")


@dataclass(frozen=True, slots=True)
class CommittedDocument:
    result: DocumentResult
    relative_path: str | None
    frozen: bool
    size: int
    checksum: str


@dataclass(frozen=True, slots=True)
class InternalRecoveryItem:
    job_id: str
    message: str
    command: DocumentCommand | None = None
    source_path: str | None = None
    output_path: str | None = None


@dataclass(frozen=True, slots=True)
class InternalRecoveryReport:
    recovered: int
    pending: int
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MaintenanceTickHint:
    report: InternalRecoveryReport
    progressed: bool
    next_due: float | None = None


class DocumentRepository(Protocol):
    def source(self, command: DocumentCommand) -> DocumentSource: ...
    def committed(self, command: DocumentCommand) -> CommittedDocument | None: ...
    def finalize(
        self, command: DocumentCommand, expected: DocumentSource, capture: InternalCapture
    ) -> DocumentResult: ...
    def path_registered(self, relative_path: str) -> bool: ...


class InternalDocumentStorage(Protocol):
    coordinator: FileOperationCoordinator

    def capture(
        self, command: DocumentCommand, source: DocumentSource, cancel_requested: Callable[[], bool]
    ) -> AbstractContextManager[InternalCapture]: ...
    def resume_capture(
        self, command: DocumentCommand, source: DocumentSource, canceled: Callable[[], bool]
    ) -> AbstractContextManager[InternalCapture]: ...
    def publish(self, capture: InternalCapture) -> None: ...
    def finish(self, capture: InternalCapture) -> None: ...
    def protect_pending(self, job_id: str) -> None: ...
    def abort_unpublished(
        self, command: DocumentCommand, path_registered: Callable[[str], bool]
    ) -> None: ...
    def verify_committed(
        self,
        command: DocumentCommand,
        committed: CommittedDocument,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> str: ...


class AttachmentDocumentService:
    """Backend is intentionally not exposed until frozen-view/cache UI is ready."""

    def __init__(self, repository: DocumentRepository, storage: InternalDocumentStorage) -> None:
        self.repository = repository
        self.storage = storage
        self.coordinator = storage.coordinator

    def execute(
        self, command: DocumentCommand, *, cancel_requested: Callable[[], bool] | None = None
    ) -> DocumentResult:
        canceled = cancel_requested or (lambda: False)
        try:
            with self.coordinator.job(canceled):
                committed = self.repository.committed(command)
                if committed is not None:
                    warning = self.storage.verify_committed(
                        command, committed, cancel_requested=canceled
                    )
                    return replace(committed.result, already_committed=True, warning=warning)
                source = self.repository.source(command)
                if command.operation is DocumentOperation.WORK:
                    validate_display_name(command.filename, extension=Path(source.name).suffix)
                elif command.operation is DocumentOperation.RELEASE_ORIGINAL:
                    if source.kind != "attachment" or source.role != "original":
                        raise ValueError("현재 원본 첨부만 지정 해제할 수 있습니다.")
                elif source.role not in {"general", "work"}:
                    raise ValueError("일반 첨부나 작업본에서만 원본/보관본을 만들 수 있습니다.")
                with self.coordinator.pin(source.relative_path):
                    try:
                        with self.storage.capture(command, source, canceled) as capture:
                            if canceled():
                                raise AttachmentCanceledError("파일 생성 요청을 취소했습니다.")
                            with self.coordinator.gate:
                                # File bytes are captured outside the gate/DB transaction;
                                # source handles remain held until the final DB commit.
                                if self.repository.source(command) != source:
                                    raise AttachmentOperationError(
                                        "출처 정보가 변경되었습니다. 다시 확인하세요."
                                    )
                                self.storage.publish(capture)
                                result = self.repository.finalize(command, source, capture)
                            try:
                                self.storage.finish(capture)
                            except Exception:
                                self.storage.protect_pending(command.job_id)
                                result = replace(
                                    result,
                                    warning="파일은 등록되었지만 작업 기록 정리 확인이 필요합니다.",
                                )
                            return result
                    except BaseException as error:
                        # In particular, a failed/ambiguous COMMIT is never permission
                        # to delete a possibly committed published file.
                        if isinstance(error, Exception):
                            try:
                                if self.repository.committed(command) is None:
                                    self.storage.abort_unpublished(
                                        command, self.repository.path_registered
                                    )
                            except Exception:
                                # Unknown/partial/published materials remain protected.
                                logging.getLogger(__name__).warning(
                                    "미확정 내부 작업 자료의 정리를 보류했습니다."
                                )
                        self.storage.protect_pending(command.job_id)
                        raise
        except FileJobCanceledError as error:
            raise AttachmentCanceledError(str(error)) from error

    def recover_confirmed(
        self,
        items: tuple[InternalRecoveryItem, ...],
        *,
        limit: int = 20,
        cancel_requested: Callable[[], bool] | None = None,
        background: bool = False,
    ) -> InternalRecoveryReport:
        """Worker-only reconciliation. Never replay/create from a pending journal."""
        canceled = cancel_requested or (lambda: False)
        recovered = 0
        warnings: list[str] = []
        for item in items[:limit]:
            if canceled():
                break
            if item.command is None:
                warnings.append(item.message)
                continue
            try:
                with self.coordinator.job(canceled, background=background):
                    committed = self.repository.committed(item.command)
                    if committed is None:
                        warnings.append(
                            "미확정 내부 파일을 보존했습니다. 자동 재등록/삭제하지 않았습니다."
                        )
                        continue
                    warning = self.storage.verify_committed(
                        item.command, committed, cancel_requested=canceled
                    )
                    if warning:
                        warnings.append(warning)
                    else:
                        recovered += 1
            except Exception:
                self.storage.protect_pending(item.job_id)
                warnings.append("내부 작업의 DB 확정 여부 확인이 필요합니다. 파일을 보존했습니다.")
        return InternalRecoveryReport(recovered, len(items) - recovered, tuple(warnings))

    def retry_pending(
        self, command: DocumentCommand, canceled: Callable[[], bool]
    ) -> DocumentResult:
        """Explicit consent; same receipt ID, no new command and no automatic replay."""
        with self.coordinator.job(canceled):
            committed = self.repository.committed(command)
            if committed:
                warning = self.storage.verify_committed(
                    command, committed, cancel_requested=canceled
                )
                return replace(committed.result, already_committed=True, warning=warning)
            source = self.repository.source(command)
            owner = "internal:" + command.job_id
            with (
                self.coordinator.pin(source.relative_path, owner=owner),
                self.storage.resume_capture(command, source, canceled) as capture,
            ):
                with self.coordinator.gate:
                    if self.repository.source(command) != source or self.repository.path_registered(
                        capture.relative_path
                    ):
                        raise ValueError("출처/파일 참조가 달라 확정하지 않았습니다.")
                    # Resumed published output is already protected by its verified handle.
                    if (
                        not getattr(self.storage, "root", Path())
                        .joinpath(capture.relative_path)
                        .exists()
                    ):
                        self.storage.publish(capture)
                    result = self.repository.finalize(command, source, capture)
                try:
                    self.storage.finish(capture)
                except Exception:
                    self.storage.protect_pending(command.job_id)
                    return replace(
                        result, warning="등록은 완료했습니다. 남은 작업 자료를 확인하세요."
                    )
                return result
