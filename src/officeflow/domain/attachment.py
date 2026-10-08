from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath


class AttachmentValidationError(ValueError):
    """Raised when attachment metadata violates a safety invariant."""


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class AttachmentMove(StrEnum):
    UP = "up"
    DOWN = "down"
    FIRST = "first"
    LAST = "last"


class AttachmentCheckStatus(StrEnum):
    UNCHANGED = "unchanged"
    MODIFIED = "modified"
    NO_BASELINE = "no_baseline"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"
    CHANGING = "changing"


@dataclass(frozen=True, slots=True)
class FileObservation:
    size_bytes: int
    checksum: str
    mtime_ns: int
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.size_bytes < 0 or self.mtime_ns < 0 or not _SHA256_PATTERN.fullmatch(self.checksum) or self.observed_at.tzinfo is None:
            raise AttachmentValidationError("파일 관측 정보가 올바르지 않습니다.")


@dataclass(frozen=True, slots=True)
class Attachment:
    id: int | None
    task_id: int
    original_name: str
    stored_name: str
    relative_path: str
    size_bytes: int
    checksum: str | None
    created_at: datetime
    missing_at: datetime | None = None
    detached_at: datetime | None = None
    sort_order: int = 0
    observed_size_bytes: int | None = None
    observed_checksum: str | None = None
    observed_mtime_ns: int | None = None
    observed_at: datetime | None = None
    last_check_status: AttachmentCheckStatus | None = None
    last_checked_at: datetime | None = None
    # Transient operation feedback, not an error log stored in the database.
    operation_warning: str = ""

    @property
    def display_size_bytes(self) -> int:
        return self.observed_size_bytes if self.observed_size_bytes is not None else self.size_bytes

    @property
    def check_label(self) -> str:
        return {None: "내용 미확인", AttachmentCheckStatus.UNCHANGED: "첨부 당시와 같음", AttachmentCheckStatus.MODIFIED: "첨부 후 수정됨", AttachmentCheckStatus.NO_BASELINE: "내용 확인됨 · 최초 기준 없음", AttachmentCheckStatus.MISSING: "파일 누락", AttachmentCheckStatus.UNAVAILABLE: "확인 불가", AttachmentCheckStatus.CHANGING: "저장 중 · 다시 확인"}[self.last_check_status]

    @property
    def id_required(self) -> int:
        if self.id is None:
            raise AttachmentValidationError("저장되지 않은 첨부파일입니다.")
        return self.id

    def __post_init__(self) -> None:
        original_name = self.original_name.strip()
        if not original_name:
            raise AttachmentValidationError("첨부파일 이름이 비어 있습니다.")
        if len(original_name) > 500:
            raise AttachmentValidationError("첨부파일 이름은 500자 이하여야 합니다.")
        if not self.stored_name or any(mark in self.stored_name for mark in ("/", "\\")):
            raise AttachmentValidationError("첨부파일 내부 이름이 올바르지 않습니다.")
        path = PurePosixPath(self.relative_path)
        if (
            not self.relative_path
            or path.is_absolute()
            or "\\" in self.relative_path
            or ".." in path.parts
            or path.name != self.stored_name
        ):
            raise AttachmentValidationError("첨부파일 상대 경로가 올바르지 않습니다.")
        if self.size_bytes < 0:
            raise AttachmentValidationError("첨부파일 크기는 0 이상이어야 합니다.")
        if not isinstance(self.sort_order, int) or isinstance(self.sort_order, bool) or self.sort_order < 0:
            raise AttachmentValidationError("첨부 순서는 0 이상의 정수여야 합니다.")
        checksum = self.checksum.lower() if self.checksum is not None else None
        if checksum is not None and not _SHA256_PATTERN.fullmatch(checksum):
            raise AttachmentValidationError("첨부파일 체크섬이 올바르지 않습니다.")
        object.__setattr__(self, "original_name", original_name)
        object.__setattr__(self, "checksum", checksum)
        observations = (self.observed_size_bytes, self.observed_checksum, self.observed_mtime_ns, self.observed_at)
        if any(value is not None for value in observations):
            if any(value is None for value in observations):
                raise AttachmentValidationError("파일 관측 정보는 한 번의 확인 결과로 함께 저장해야 합니다.")
            assert self.observed_size_bytes is not None and self.observed_checksum is not None and self.observed_mtime_ns is not None and self.observed_at is not None
            FileObservation(self.observed_size_bytes, self.observed_checksum, self.observed_mtime_ns, self.observed_at)
        if self.last_check_status is not None:
            object.__setattr__(self, "last_check_status", AttachmentCheckStatus(self.last_check_status))

        for label, value in (
            ("생성 시각", self.created_at),
            ("누락 확인 시각", self.missing_at),
            ("연결 해제 시각", self.detached_at),
            ("최근 확인 시각", self.last_checked_at),
        ):
            if value is not None and value.tzinfo is None:
                raise AttachmentValidationError(f"첨부파일 {label}에는 시간대가 필요합니다.")

    def mark_missing(self, missing_at: datetime | None) -> Attachment:
        return replace(self, missing_at=missing_at)

    def mark_detached(self, detached_at: datetime | None) -> Attachment:
        return replace(self, detached_at=detached_at)
