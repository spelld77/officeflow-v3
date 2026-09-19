from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import PurePosixPath


class AttachmentValidationError(ValueError):
    """Raised when attachment metadata violates a safety invariant."""


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


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
        checksum = self.checksum.lower() if self.checksum is not None else None
        if checksum is not None and not _SHA256_PATTERN.fullmatch(checksum):
            raise AttachmentValidationError("첨부파일 체크섬이 올바르지 않습니다.")
        object.__setattr__(self, "original_name", original_name)
        object.__setattr__(self, "checksum", checksum)

        for label, value in (
            ("생성 시각", self.created_at),
            ("누락 확인 시각", self.missing_at),
            ("연결 해제 시각", self.detached_at),
        ):
            if value is not None and value.tzinfo is None:
                raise AttachmentValidationError(f"첨부파일 {label}에는 시간대가 필요합니다.")

    def mark_missing(self, missing_at: datetime | None) -> Attachment:
        return replace(self, missing_at=missing_at)

    def mark_detached(self, detached_at: datetime | None) -> Attachment:
        return replace(self, detached_at=detached_at)
