from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from officeflow.domain.enums import TaskStatus


class AttachmentSearchInterrupted(RuntimeError):
    """A search was cancelled or exceeded its time budget."""


class AttachmentKind(StrEnum):
    ALL = "all"
    EXCEL = "excel"
    PDF = "pdf"
    WORD = "word"
    IMAGE = "image"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class AttachmentCursor:
    created_at: datetime
    attachment_id: int


@dataclass(frozen=True, slots=True)
class AttachmentSearchQuery:
    search: str = ""
    kind: AttachmentKind = AttachmentKind.ALL
    attached_after: datetime | None = None
    attached_before: datetime | None = None
    include_trash: bool = False
    include_detached: bool = False
    cursor: AttachmentCursor | None = None
    limit: int = 50

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 500:
            raise ValueError("한 번에 조회할 파일은 1~500개여야 합니다.")
        if self.search.strip() and len(self.search.strip()) < 2:
            raise ValueError("파일명은 2글자 이상 입력해 주세요.")
        for value in (self.attached_after, self.attached_before):
            if value is not None and value.tzinfo is None:
                raise ValueError("첨부일 범위에 시간대가 필요합니다.")
        if (
            self.attached_after is not None
            and self.attached_before is not None
            and self.attached_after >= self.attached_before
        ):
            raise ValueError("첨부일 시작은 종료보다 빨라야 합니다.")


@dataclass(frozen=True, slots=True)
class AttachmentSearchHit:
    attachment_id: int
    task_id: int
    task_title: str
    task_status: TaskStatus
    original_name: str
    size_bytes: int
    created_at: datetime
    deleted: bool
    detached: bool
    missing: bool


@dataclass(frozen=True, slots=True)
class AttachmentSearchPage:
    items: tuple[AttachmentSearchHit, ...]
    next_cursor: AttachmentCursor | None
    has_more: bool


class AttachmentSearchRepository(Protocol):
    def search_page(
        self,
        query: AttachmentSearchQuery,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AttachmentSearchPage: ...


class AttachmentSearchService:
    def __init__(self, repository: AttachmentSearchRepository) -> None:
        self._repository = repository

    def search_page(
        self,
        query: AttachmentSearchQuery,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AttachmentSearchPage:
        return self._repository.search_page(query, cancel_requested=cancel_requested)
