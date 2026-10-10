"""Readable independent bundles. No internal deletion, restoration, or file observation writes."""

import hashlib
import json
import os
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from officeflow.application.attachment_export import AttachmentExportService, FileReference
from officeflow.application.attachments import AttachmentCanceledError
from officeflow.application.exporting import ExportCanceledError
from officeflow.application.file_operations import FileJobCanceledError


class ArchiveScope(StrEnum):
    PERIOD = "period"
    SELECTED = "selected"


class ArchiveDateBasis(StrEnum):
    CREATED = "created_at"
    COMPLETED = "completed_at"
    UPDATED = "updated_at"


@dataclass(frozen=True, slots=True)
class ArchiveOptions:
    start: date
    end: date
    timezone: str = "Asia/Seoul"
    scope: ArchiveScope = ArchiveScope.PERIOD
    basis: ArchiveDateBasis = ArchiveDateBasis.CREATED
    completed_only: bool = True
    task_ids: tuple[int, ...] = ()
    attachments: bool = True
    versions: bool = False
    snapshot_files: bool = False

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("종료일은 시작일 이후로 지정하세요.")
        if self.scope == ArchiveScope.SELECTED and not self.task_ids:
            raise ValueError("보관할 업무를 먼저 목록에서 선택하세요.")
        if len(self.task_ids) > 5000:
            raise ValueError("한 묶음에 최대 5,000개 업무를 보관할 수 있습니다.")

    @property
    def description(self) -> str:
        if self.scope == ArchiveScope.SELECTED:
            return "선택 업무 · 날짜/주 화면 검색·필터 미적용"
        basis = {
            ArchiveDateBasis.CREATED: "등록일",
            ArchiveDateBasis.COMPLETED: "완료일",
            ArchiveDateBasis.UPDATED: "최근 수정일",
        }[self.basis]
        return f"{self.start} ~ {self.end} · {basis} · {'완료 업무만' if self.completed_only else '모든 상태'} · {self.timezone}"


@dataclass(frozen=True, slots=True)
class ArchiveFile:
    reference: FileReference
    name: str
    role: str
    memo: str
    label: str
    source_token: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArchiveTask:
    id: int
    title: str
    status: str
    priority: str
    description: str
    result: str
    schedule: str
    dates: str
    sections: tuple[tuple[str, str], ...]
    files: tuple[ArchiveFile, ...]


class ArchiveRepository(Protocol):
    def task_ids(self, options: ArchiveOptions) -> tuple[int, ...]: ...
    def capture(self, task_id: int, options: ArchiveOptions) -> ArchiveTask: ...


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    folder: Path
    task_count: int
    file_count: int
    warning: str = ""


class ArchiveBundleError(RuntimeError):
    pass


class ArchiveCanceledError(ArchiveBundleError, ExportCanceledError):
    pass


class ArchiveWriter(Protocol):
    def start(self, folder: Path, options: ArchiveOptions, created: datetime) -> None: ...
    def task(
        self, item: ArchiveTask, captured: datetime, files: tuple[tuple[ArchiveFile, str], ...]
    ) -> None: ...
    def finish(self) -> None: ...
    def close(self) -> None: ...


class TaskArchiveService:
    def __init__(
        self,
        repository: ArchiveRepository,
        files: AttachmentExportService,
        writer_factory: Callable[[], ArchiveWriter],
    ) -> None:
        self.repository, self.files, self.writer_factory = repository, files, writer_factory

    def export(
        self,
        parent: Path,
        options: ArchiveOptions,
        *,
        canceled: Callable[[], bool] = lambda: False,
        progress: Callable[[int, int], None] | None = None,
    ) -> ArchiveResult:
        folder: Path | None = None
        writer = self.writer_factory()
        total_files = 0
        try:
            with self.files.attachments.coordinator.job(canceled):
                identities = self.repository.task_ids(options)
                if not identities:
                    raise ValueError("선택한 범위에 보관할 업무가 없습니다.")
                if canceled():
                    raise ExportCanceledError("업무 보관을 취소했습니다.")
                parent = parent.resolve(strict=True)
                if not parent.is_dir():
                    raise ValueError("보관할 외부 폴더를 선택하세요.")
                root = self.files.storage.data_root
                if parent.is_relative_to(root) or parent.is_relative_to(
                    self.files.attachments.storage_root
                ):
                    raise ValueError("OfficeFlow 관리 폴더에는 외부 묶음을 만들 수 없습니다.")
                created = datetime.now(UTC)
                folder = (
                    parent
                    / f"OfficeFlow_{options.start.year}_{created:%Y%m%d_%H%M%S}_{uuid4().hex[:10]}"
                )
                folder.mkdir(exist_ok=False)
                self._write(
                    folder / "미완료.txt",
                    "이 보관 묶음은 아직 완료되지 않았습니다. 내부 원본은 유지됩니다.\n",
                )
                writer.start(folder, options, created)
                # Stream the manifest too; don't accumulate all file metadata in RAM.
                with (folder / "파일목록.jsonl").open("x", encoding="utf-8") as manifest:
                    for done, identity in enumerate(identities, 1):
                        if canceled():
                            raise ExportCanceledError("업무 보관을 취소했습니다.")
                        with ExitStack() as pins:
                            with self.files.attachments.coordinator.gate:
                                item = self.repository.capture(identity, options)
                                resolved = tuple(
                                    self.files.resolve(file.reference) for file in item.files
                                )
                                for source in resolved:
                                    pins.enter_context(
                                        self.files.attachments.coordinator.pin(source.relative_path)
                                    )
                            captured = datetime.now(UTC)
                            saved: list[tuple[ArchiveFile, str]] = []
                            for metadata, source in zip(item.files, resolved, strict=True):
                                if canceled():
                                    raise ExportCanceledError("업무 보관을 취소했습니다.")
                                directory = folder / "첨부파일" / str(identity)
                                directory.mkdir(parents=True, exist_ok=True)
                                target = self.files.storage.validate_destination(
                                    directory / self.files.storage.safe_name(source.name),
                                    source.path,
                                )
                                prepared = self.files.storage.prepare(
                                    source,
                                    target,
                                    uuid4().hex,
                                    cancel_requested=canceled,
                                    progress=None,
                                )
                                try:
                                    if (
                                        source.attachment
                                        and source.attachment.is_frozen
                                        and (
                                            prepared.observation.checksum
                                            != source.attachment.fixed_checksum
                                            or prepared.observation.size_bytes
                                            != source.attachment.fixed_size_bytes
                                        )
                                    ):
                                        raise ValueError(
                                            f"고정 파일의 기준과 내용이 다릅니다: {metadata.name}"
                                        )
                                    output, warning = self.files.storage.publish(
                                        prepared,
                                        numbered=True,
                                        cancel_requested=canceled,
                                        confirm_overwrite=None,
                                    )
                                except Exception:
                                    # Publication journals own their paths; use their existing safe cleanup only.
                                    self.files.storage.abort(prepared)
                                    raise
                                if warning:
                                    raise ArchiveBundleError(warning)
                                relative = output.relative_to(folder).as_posix()
                                saved.append((metadata, relative))
                                manifest.write(
                                    json.dumps(
                                        {
                                            "task_id": identity,
                                            "kind": metadata.reference.kind.value,
                                            "id": metadata.reference.attachment_id,
                                            "name": metadata.name,
                                            "role": metadata.role,
                                            "memo": metadata.memo,
                                            "label": metadata.label,
                                            "path": relative,
                                            "sha256": prepared.observation.checksum,
                                            "size": prepared.observation.size_bytes,
                                            "captured_at": prepared.observation.observed_at.isoformat(),
                                        },
                                        ensure_ascii=False,
                                    )
                                    + "\n"
                                )
                                total_files += 1
                            if self.repository.capture(identity, options) != item:
                                raise ArchiveBundleError(
                                    "보관 중 업무 내용이 변경되었습니다. 다시 보관하세요."
                                )
                            writer.task(item, captured, tuple(saved))
                        if progress:
                            progress(done, len(identities))
                    manifest.flush()
                    os.fsync(manifest.fileno())
                if canceled():
                    raise ExportCanceledError("업무 보관을 취소했습니다.")
                writer.finish()
                writer.close()
                outputs = {}
                for name in ("업무목록.html", "업무목록.xlsx", "파일목록.jsonl"):
                    digest = hashlib.sha256()
                    with (folder / name).open("rb") as stream:
                        while chunk := stream.read(1024 * 1024):
                            if canceled():
                                raise ExportCanceledError("업무 보관을 취소했습니다.")
                            digest.update(chunk)
                    outputs[name] = digest.hexdigest()
                self._write(
                    folder / "보관정보.json",
                    json.dumps(
                        {
                            "format": 1,
                            "created_at": created.isoformat(),
                            "scope": options.description,
                            "task_ids": identities,
                            "task_count": len(identities),
                            "file_count": total_files,
                            "include": {
                                "attachments": options.attachments,
                                "versions": options.versions,
                                "snapshot_files": options.snapshot_files,
                            },
                            "excluded": [
                                "휴지통",
                                "해제 첨부",
                                "보기 캐시",
                                "자동 이력",
                                "중요시점 정보 전체",
                                "외부 폴더 내용",
                            ],
                            "sha256": outputs,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
                self._write(
                    folder / ".완료.txt.part",
                    "업무 묶음 보관 완료. 업무목록.html을 열어 조회하세요.\n전체 백업/복원 파일은 아닙니다. 내부 자료는 삭제하지 않았습니다.\n",
                )
                # A failed write/fsync must not leave a plausible complete marker.
                (folder / ".완료.txt.part").rename(folder / "완료.txt")
                warning = ""
                try:
                    (folder / "미완료.txt").unlink(missing_ok=True)
                except OSError:
                    warning = "자료 보관은 완료했지만 이전 미완료 표식을 정리하지 못했습니다. 완료.txt와 보관정보.json을 확인하세요."
                return ArchiveResult(folder, len(identities), total_files, warning)
        except Exception as error:
            error_type = (
                ArchiveCanceledError
                if canceled()
                or isinstance(
                    error, (ExportCanceledError, AttachmentCanceledError, FileJobCanceledError)
                )
                else ArchiveBundleError
            )
            if folder is not None:
                raise error_type(
                    f"보관이 완료되지 않았습니다: {error}\n미완료 폴더: {folder}\n내부 업무/파일은 유지됩니다."
                ) from error
            if error_type is ArchiveCanceledError:
                raise ArchiveCanceledError("업무 보관을 취소했습니다.") from error
            raise
        finally:
            writer.close()

    @staticmethod
    def _write(path: Path, text: str) -> None:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
