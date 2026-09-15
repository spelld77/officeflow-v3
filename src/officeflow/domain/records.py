from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from officeflow.domain.enums import TaskPriority


class RecordValidationError(ValueError):
    """Raised when checklist or work-log content is invalid."""


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    id: int | None
    task_id: int
    content: str
    is_done: bool
    position: int
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        normalized = self.content.strip()
        if not normalized:
            raise RecordValidationError("체크리스트 내용을 입력하세요.")
        if len(normalized) > 500:
            raise RecordValidationError("체크리스트 내용은 500자 이하여야 합니다.")
        if self.position < 0:
            raise RecordValidationError("체크리스트 순서는 0 이상이어야 합니다.")
        object.__setattr__(self, "content", normalized)

    def edit(self, content: str) -> ChecklistItem:
        return replace(self, content=content)

    def set_done(self, is_done: bool, *, now: datetime | None = None) -> ChecklistItem:
        if is_done == self.is_done:
            return self
        return replace(
            self,
            is_done=is_done,
            completed_at=(now or datetime.now(UTC)) if is_done else None,
        )


@dataclass(frozen=True, slots=True)
class WorkLog:
    id: int | None
    task_id: int | None
    occurrence_id: int | None
    log_date: date
    content: str
    result: str
    priority_snapshot: TaskPriority
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        normalized = self.content.strip()
        if not normalized:
            raise RecordValidationError("업무일지 내용을 입력하세요.")
        object.__setattr__(self, "content", normalized)
        object.__setattr__(self, "result", self.result.strip())

    @classmethod
    def create(
        cls,
        *,
        task_id: int | None,
        occurrence_id: int | None,
        log_date: date,
        content: str,
        result: str = "",
        priority_snapshot: TaskPriority = TaskPriority.NORMAL,
        now: datetime | None = None,
    ) -> WorkLog:
        timestamp = now or datetime.now(UTC)
        return cls(
            id=None,
            task_id=task_id,
            occurrence_id=occurrence_id,
            log_date=log_date,
            content=content,
            result=result,
            priority_snapshot=priority_snapshot,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def edit(
        self,
        *,
        log_date: date,
        content: str,
        result: str,
        now: datetime | None = None,
    ) -> WorkLog:
        return replace(
            self,
            log_date=log_date,
            content=content,
            result=result,
            updated_at=now or datetime.now(UTC),
        )
