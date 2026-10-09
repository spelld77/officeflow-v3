"""Independent external copies; exporting never unlinks or deletes an attachment."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol

from officeflow.application.attachments import (
    AttachmentCanceledError,
    AttachmentOperationError,
    AttachmentService,
)
from officeflow.application.file_operations import FileJobCanceledError
from officeflow.domain.attachment import Attachment, FileObservation


class FileReferenceKind(StrEnum):
    ATTACHMENT = "attachment"
    VERSION = "version"
    CACHE = "cache"
    ORPHAN = "orphan"


@dataclass(frozen=True, slots=True)
class FileReference:
    kind: FileReferenceKind
    attachment_id: int | None = None
    relative_path: str | None = None

    def __post_init__(self) -> None:
        if self.kind in {FileReferenceKind.ATTACHMENT, FileReferenceKind.VERSION}:
            if not self.attachment_id or self.relative_path is not None:
                raise ValueError("올바른 첨부 ID가 필요합니다.")
        elif self.kind in {FileReferenceKind.ORPHAN, FileReferenceKind.CACHE}:
            path = PurePosixPath(self.relative_path or "")
            if (
                self.attachment_id is not None
                or not self.relative_path
                or path.is_absolute()
                or "\\" in self.relative_path
                or ".." in path.parts
                or any(part.startswith(".") for part in path.parts)
            ):
                raise ValueError("올바른 관리 파일 상대 경로가 필요합니다.")
            if self.kind is FileReferenceKind.CACHE and not (
                len(path.parts) == 5 and path.parts[:2] == ("cache", "attachment-views")
            ):
                raise ValueError("소유된 보기 사본 파일을 선택하세요.")
        else:
            raise ValueError("지원하지 않는 파일 참조입니다.")

    @classmethod
    def attachment(cls, attachment_id: int) -> FileReference:
        return cls(FileReferenceKind.ATTACHMENT, attachment_id=attachment_id)

    @classmethod
    def version(cls, version_id: int) -> FileReference:
        return cls(FileReferenceKind.VERSION, attachment_id=version_id)

    @classmethod
    def cache(cls, relative: str) -> FileReference:
        return cls(FileReferenceKind.CACHE, relative_path=relative)

    @classmethod
    def orphan(cls, relative_path: str) -> FileReference:
        return cls(FileReferenceKind.ORPHAN, relative_path=relative_path)


@dataclass(frozen=True, slots=True)
class ResolvedFileReference:
    reference: FileReference
    path: Path
    name: str
    relative_path: str
    attachment: Attachment | None = None


class ExportStatus(StrEnum):
    SAVED = "saved"
    FAILED = "failed"
    UNPROCESSED = "unprocessed"
    NEEDS_REVIEW = "needs_review"


class ExportDestinationError(OSError):
    """Creating a file in the selected destination folder failed."""


@dataclass(frozen=True, slots=True)
class OverwritePrompt:
    path: Path
    size_bytes: int
    checksum: str
    changed: bool = False


@dataclass(frozen=True, slots=True)
class PreparedExport:
    job_id: str
    temporary: Path
    destination: Path
    journal: Path
    observation: FileObservation
    source_identity: tuple[int, int]
    temporary_identity: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class ExportRecoveryItem:
    journal: Path
    signature: str
    message: str
    destination: Path | None = None
    temporary: Path | None = None
    backup: Path | None = None
    snapshots: tuple[tuple[Path, str, tuple[int, int, int, int]], ...] = ()
    can_cleanup: bool = False
    can_restore: bool = False


@dataclass(frozen=True, slots=True)
class ExportItemResult:
    reference: FileReference
    name: str
    status: ExportStatus
    output_path: Path | None = None
    captured_at: datetime | None = None
    message: str = ""
    journal_path: Path | None = None


@dataclass(frozen=True, slots=True)
class ExportBatchResult:
    job_id: str
    items: tuple[ExportItemResult, ...]
    folder: Path
    canceled: bool = False

    @property
    def retry_references(self) -> tuple[FileReference, ...]:
        return tuple(
            item.reference
            for item in self.items
            if item.status in {ExportStatus.FAILED, ExportStatus.UNPROCESSED}
        )


class ExportStorage(Protocol):
    data_root: Path

    def new_job_id(self) -> str: ...
    def validate_destination(self, path: Path, source: Path) -> Path: ...
    def prepare(
        self,
        resolved: ResolvedFileReference,
        destination: Path,
        job_id: str,
        *,
        cancel_requested: Callable[[], bool],
        progress: Callable[[int, int], None] | None,
    ) -> PreparedExport: ...
    def publish(
        self,
        prepared: PreparedExport,
        *,
        numbered: bool,
        cancel_requested: Callable[[], bool],
        confirm_overwrite: Callable[[OverwritePrompt], bool] | None,
    ) -> tuple[Path, str]: ...
    def abort(self, prepared: PreparedExport) -> None: ...
    def safe_name(self, name: str) -> str: ...
    def pending_operations(self) -> tuple[str, ...]: ...
    def inspect_recovery(
        self, cancel_requested: Callable[[], bool]
    ) -> tuple[ExportRecoveryItem, ...]: ...
    def recover(self, item: ExportRecoveryItem, *, restore: bool) -> str: ...


class AttachmentExportService:
    def __init__(self, attachments: AttachmentService, storage: ExportStorage) -> None:
        self.attachments = attachments
        self.storage = storage

    @staticmethod
    def _version_name(name: str, label: str) -> str:
        path = PurePosixPath(name)
        label = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", label).rstrip(" .") or "보관본"
        return f"{path.stem} - {label}{path.suffix}"

    def resolve(self, reference: FileReference) -> ResolvedFileReference:
        root = self.attachments.storage_root
        if reference.kind is FileReferenceKind.VERSION:
            if self.attachments.lifecycle is None or reference.attachment_id is None:
                raise ValueError("보관본 서비스가 준비되지 않았습니다.")
            entry = self.attachments.lifecycle.catalog.get("version", reference.attachment_id)
            attachment = entry.file
            relative, name = attachment.relative_path, self._version_name(entry.name, entry.label)
        elif reference.kind is FileReferenceKind.ATTACHMENT:
            assert reference.attachment_id is not None
            attachment = self.attachments.get_metadata(reference.attachment_id)
            relative, name = attachment.relative_path, attachment.name
        elif reference.kind is FileReferenceKind.CACHE:
            if not self.attachments.lifecycle or not reference.relative_path:
                raise ValueError("보기 사본 서비스를 확인하세요.")
            path = root / reference.relative_path
            copy = self.attachments.lifecycle.cache.by_path(path)
            if copy is None or copy.relative_path != reference.relative_path:
                raise ValueError("보기 사본의 소유 원장을 확인하세요.")
            relative, name, attachment = copy.relative_path, copy.name, None
        else:
            attachment = None
            assert reference.relative_path is not None
            relative, name = reference.relative_path, PurePosixPath(reference.relative_path).name
            if self.attachments.is_path_registered(relative):
                raise AttachmentOperationError(
                    "이 파일은 다시 첨부로 등록되었습니다. 정리 목록을 갱신하세요."
                )
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise AttachmentOperationError("파일이 없거나 관리 폴더를 벗어납니다.")
        return ResolvedFileReference(reference, path, name, relative, attachment)

    def export(
        self,
        references: tuple[FileReference, ...],
        target: Path,
        *,
        single: bool = False,
        cancel_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int], None] | None = None,
        confirm_overwrite: Callable[[OverwritePrompt], bool] | None = None,
    ) -> ExportBatchResult:
        if (
            not references
            or (single and len(references) != 1)
            or len(set(references)) != len(references)
        ):
            raise ValueError("저장할 파일을 선택하세요. 단일 저장은 한 개만 가능합니다.")
        if (
            not single
            and len(references) > 1
            and (
                any(ref.attachment_id is None for ref in references)
                or len({self.task_id(ref) for ref in references if ref.attachment_id is not None})
                != 1
            )
        ):
            raise ValueError("여러 파일 저장은 한 업무의 첨부 안에서만 가능합니다.")
        canceled = cancel_requested or (lambda: False)
        job_id = self.storage.new_job_id()
        items: list[ExportItemResult] = []
        folder = target.parent if single else target
        try:
            with self.attachments.coordinator.job(canceled):
                stop = False
                for reference in references:
                    if stop or canceled():
                        items.append(
                            ExportItemResult(
                                reference,
                                "",
                                ExportStatus.UNPROCESSED,
                                message="취소 또는 저장 폴더 사용 불가",
                            )
                        )
                        continue
                    prepared: PreparedExport | None = None
                    name = ""
                    try:
                        with self.attachments.coordinator.gate:
                            resolved = self.resolve(reference)
                            name = resolved.name
                            reservation = self.attachments.coordinator.pin(resolved.relative_path)
                            reservation.__enter__()
                        try:
                            destination = (
                                target if single else folder / self.storage.safe_name(name)
                            )
                            destination = self.storage.validate_destination(
                                destination, resolved.path
                            )
                            if destination.is_relative_to(self.attachments.storage_root):
                                raise AttachmentOperationError(
                                    "관리 첨부 폴더에는 외부 사본을 저장할 수 없습니다."
                                )
                            prepared = self.storage.prepare(
                                resolved,
                                destination,
                                job_id,
                                cancel_requested=canceled,
                                progress=progress,
                            )
                            if (
                                resolved.attachment
                                and resolved.attachment.is_frozen
                                and (
                                    prepared.observation.checksum
                                    != resolved.attachment.fixed_checksum
                                    or prepared.observation.size_bytes
                                    != resolved.attachment.fixed_size_bytes
                                )
                            ):
                                self.storage.abort(prepared)
                                prepared = None
                                raise AttachmentOperationError(
                                    "보관 후 내용이 변경되어 저장하지 않았습니다. 기준은 바꾸지 않았습니다."
                                )
                            warning = ""
                            if (
                                resolved.attachment
                                and reference.kind is FileReferenceKind.ATTACHMENT
                            ):
                                try:
                                    self.attachments.record_observation(
                                        resolved.attachment, prepared.observation
                                    )
                                except Exception:
                                    warning = "사본 준비 완료 · 첨부 정보는 다시 확인 필요"
                        finally:
                            reservation.__exit__(None, None, None)
                        output, publication_warning = self.storage.publish(
                            prepared,
                            numbered=not single,
                            cancel_requested=canceled,
                            confirm_overwrite=confirm_overwrite,
                        )
                        items.append(
                            ExportItemResult(
                                reference,
                                name,
                                ExportStatus.SAVED,
                                output,
                                prepared.observation.observed_at,
                                " · ".join(part for part in (warning, publication_warning) if part),
                                prepared.journal if publication_warning else None,
                            )
                        )
                    except AttachmentCanceledError:
                        if prepared:
                            try:
                                self.storage.abort(prepared)
                            except Exception as cleanup_error:
                                stop = True
                                items.append(
                                    ExportItemResult(
                                        reference,
                                        name,
                                        ExportStatus.NEEDS_REVIEW,
                                        message=str(cleanup_error),
                                        journal_path=prepared.journal,
                                    )
                                )
                                continue
                        stop = True
                        items.append(
                            ExportItemResult(
                                reference, name, ExportStatus.UNPROCESSED, message="취소됨"
                            )
                        )
                    except Exception as error:
                        # A journal means ambiguous publication/cleanup. Never retry
                        # an uncertain successful overwrite as an ordinary failure.
                        review = prepared is not None and prepared.journal.exists()
                        items.append(
                            ExportItemResult(
                                reference,
                                name,
                                ExportStatus.NEEDS_REVIEW if review else ExportStatus.FAILED,
                                message=str(error),
                                journal_path=prepared.journal if review and prepared else None,
                            )
                        )
                        if (
                            not folder.is_dir()
                            or isinstance(error, ExportDestinationError)
                            or (isinstance(error, OSError) and error.errno == 28)
                        ):
                            stop = True
        except FileJobCanceledError:
            items.extend(
                ExportItemResult(reference, "", ExportStatus.UNPROCESSED, message="대기 중 취소됨")
                for reference in references
            )
        return ExportBatchResult(job_id, tuple(items), folder, canceled())

    def task_id(self, reference: FileReference) -> int:
        if (
            reference.kind is FileReferenceKind.VERSION
            and self.attachments.lifecycle
            and reference.attachment_id
        ):
            return self.attachments.lifecycle.catalog.get(
                "version", reference.attachment_id
            ).task_id
        if reference.attachment_id:
            return self.attachments.get_metadata(reference.attachment_id).task_id
        raise ValueError("업무 안의 첨부/보관본을 선택하세요.")

    def name(self, reference: FileReference) -> str:
        if (
            reference.kind is FileReferenceKind.VERSION
            and self.attachments.lifecycle
            and reference.attachment_id
        ):
            entry = self.attachments.lifecycle.catalog.get("version", reference.attachment_id)
            return self._version_name(entry.name, entry.label)
        if reference.attachment_id:
            return self.attachments.get_metadata(reference.attachment_id).name
        return PurePosixPath(reference.relative_path or "첨부파일").name

    def prepare_open(self, reference: FileReference, canceled: Callable[[], bool]) -> Path:
        if reference.attachment_id and self.attachments.lifecycle:
            return self.attachments.lifecycle.prepare_open(
                reference.kind.value, reference.attachment_id, canceled
            )
        return self.resolve(reference).path

    def inspect_recovery(self, canceled: Callable[[], bool]) -> tuple[ExportRecoveryItem, ...]:
        with self.attachments.coordinator.job(canceled):
            return self.storage.inspect_recovery(canceled)

    def recover(
        self, item: ExportRecoveryItem, *, restore: bool, canceled: Callable[[], bool]
    ) -> str:
        with self.attachments.coordinator.job(canceled):
            if canceled():
                raise AttachmentCanceledError("복구를 취소했습니다.")
            return self.storage.recover(item, restore=restore)
