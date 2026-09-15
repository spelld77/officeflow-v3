from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from officeflow.domain.enums import TaskPriority, TaskStatus
from officeflow.domain.recurrence import validate_recurrence_rule


class TaskValidationError(ValueError):
    """Raised when a task violates a domain invariant."""


class InvalidStatusTransitionError(TaskValidationError):
    """Raised when a requested task status transition is not allowed."""


ALLOWED_STATUS_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.ACTIVE: frozenset(
        {TaskStatus.PENDING, TaskStatus.COMPLETED, TaskStatus.CANCELED, TaskStatus.ARCHIVED}
    ),
    TaskStatus.PENDING: frozenset(
        {TaskStatus.ACTIVE, TaskStatus.COMPLETED, TaskStatus.CANCELED, TaskStatus.ARCHIVED}
    ),
    TaskStatus.COMPLETED: frozenset({TaskStatus.ACTIVE, TaskStatus.ARCHIVED}),
    TaskStatus.CANCELED: frozenset({TaskStatus.ACTIVE, TaskStatus.ARCHIVED}),
    TaskStatus.ARCHIVED: frozenset({TaskStatus.ACTIVE}),
}


@dataclass(frozen=True, slots=True)
class Task:
    id: int | None
    title: str
    description: str
    status: TaskStatus
    priority: TaskPriority
    is_pinned: bool
    all_day: bool
    starts_at: datetime | None
    ends_at: datetime | None
    timezone: str
    recurrence_rule: str | None
    result_note: str
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    legacy_id: int | None = None

    def __post_init__(self) -> None:
        normalized_title = self.title.strip()
        if not normalized_title:
            raise TaskValidationError("업무 제목을 입력하세요.")
        if len(normalized_title) > 200:
            raise TaskValidationError("업무 제목은 200자 이하여야 합니다.")
        if self.ends_at is not None and self.starts_at is None:
            raise TaskValidationError("종료 일정에는 시작 일정이 필요합니다.")
        if self.starts_at is not None and self.starts_at.tzinfo is None:
            raise TaskValidationError("시작 일정에 시간대 정보가 필요합니다.")
        if self.ends_at is not None and self.ends_at.tzinfo is None:
            raise TaskValidationError("종료 일정에 시간대 정보가 필요합니다.")
        if (
            self.ends_at is not None
            and self.starts_at is not None
            and self.ends_at <= self.starts_at
        ):
            raise TaskValidationError("종료 일정은 시작 일정보다 뒤여야 합니다.")
        if self.recurrence_rule is not None:
            if self.starts_at is None:
                raise TaskValidationError("반복 업무에는 시작 일정이 필요합니다.")
            validate_recurrence_rule(self.recurrence_rule, self.starts_at)
        object.__setattr__(self, "title", normalized_title)

    @classmethod
    def create(
        cls,
        *,
        title: str,
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        status: TaskStatus = TaskStatus.ACTIVE,
        is_pinned: bool = False,
        all_day: bool = False,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        timezone: str = "Asia/Seoul",
        recurrence_rule: str | None = None,
        now: datetime | None = None,
    ) -> Task:
        timestamp = now or datetime.now(UTC)
        return cls(
            id=None,
            title=title,
            description=description.strip(),
            status=status,
            priority=priority,
            is_pinned=is_pinned,
            all_day=all_day,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone=timezone,
            recurrence_rule=recurrence_rule,
            result_note="",
            completed_at=timestamp if status is TaskStatus.COMPLETED else None,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def update_details(
        self,
        *,
        title: str,
        description: str,
        priority: TaskPriority,
        status: TaskStatus,
        is_pinned: bool,
        all_day: bool,
        starts_at: datetime | None,
        ends_at: datetime | None,
        timezone: str,
        recurrence_rule: str | None,
        now: datetime,
    ) -> Task:
        if status is not self.status and status not in ALLOWED_STATUS_TRANSITIONS[self.status]:
            raise InvalidStatusTransitionError(
                f"{self.status.value} 상태에서 {status.value} 상태로 변경할 수 없습니다."
            )
        completed_at = self.completed_at
        if status is TaskStatus.COMPLETED and self.status is not TaskStatus.COMPLETED:
            completed_at = now
        elif status is not TaskStatus.COMPLETED:
            completed_at = None
        return replace(
            self,
            title=title,
            description=description.strip(),
            priority=priority,
            status=status,
            is_pinned=is_pinned,
            all_day=all_day,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone=timezone,
            recurrence_rule=recurrence_rule,
            completed_at=completed_at,
            updated_at=now,
        )

    def transition_to(self, status: TaskStatus, *, now: datetime) -> Task:
        if status is self.status:
            return self
        if status not in ALLOWED_STATUS_TRANSITIONS[self.status]:
            raise InvalidStatusTransitionError(
                f"{self.status.value} 상태에서 {status.value} 상태로 변경할 수 없습니다."
            )
        return replace(
            self,
            status=status,
            completed_at=now if status is TaskStatus.COMPLETED else None,
            updated_at=now,
        )

    def update_result_note(self, result_note: str, *, now: datetime) -> Task:
        return replace(self, result_note=result_note.strip(), updated_at=now)
