from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

from officeflow.domain.enums import TaskPriority, TaskStatus
from officeflow.domain.task import Task


class TaskNotFoundError(LookupError):
    """Raised when a requested task does not exist."""


class TaskView(StrEnum):
    TODAY = "today"
    UPCOMING = "upcoming"
    IMPORTANT = "important"
    PENDING = "pending"
    COMPLETED = "completed"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class TaskDraft:
    title: str
    description: str = ""
    priority: TaskPriority = TaskPriority.NORMAL
    status: TaskStatus = TaskStatus.ACTIVE
    is_pinned: bool = False
    all_day: bool = False
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    timezone: str = "Asia/Seoul"


@dataclass(frozen=True, slots=True)
class TaskSummary:
    overdue: int
    in_progress: int
    today: int
    completed_today: int


class TaskRepository(Protocol):
    def add(self, task: Task) -> Task: ...

    def update(self, task: Task) -> Task: ...

    def get(self, task_id: int) -> Task | None: ...

    def list(self, *, search: str = "") -> list[Task]: ...


class TaskService:
    def __init__(self, repository: TaskRepository, *, timezone: str = "Asia/Seoul") -> None:
        self._repository = repository
        self._timezone = timezone

    def create(self, draft: TaskDraft, *, now: datetime | None = None) -> Task:
        task = Task.create(
            title=draft.title,
            description=draft.description,
            priority=draft.priority,
            status=draft.status,
            is_pinned=draft.is_pinned,
            all_day=draft.all_day,
            starts_at=draft.starts_at,
            ends_at=draft.ends_at,
            timezone=draft.timezone,
            now=now,
        )
        return self._repository.add(task)

    def quick_add(
        self,
        title: str,
        *,
        view: TaskView,
        local_date: date | None = None,
        now: datetime | None = None,
    ) -> Task:
        starts_at: datetime | None = None
        ends_at: datetime | None = None
        all_day = False
        if view is TaskView.TODAY:
            zone = ZoneInfo(self._timezone)
            day = local_date or datetime.now(zone).date()
            starts_at = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
            ends_at = (datetime.combine(day, time.min, tzinfo=zone) + timedelta(days=1)).astimezone(
                UTC
            )
            all_day = True
        return self.create(
            TaskDraft(
                title=title,
                all_day=all_day,
                starts_at=starts_at,
                ends_at=ends_at,
                timezone=self._timezone,
            ),
            now=now,
        )

    def update(self, task_id: int, draft: TaskDraft, *, now: datetime | None = None) -> Task:
        task = self.get(task_id)
        updated = task.update_details(
            title=draft.title,
            description=draft.description,
            priority=draft.priority,
            status=draft.status,
            is_pinned=draft.is_pinned,
            all_day=draft.all_day,
            starts_at=draft.starts_at,
            ends_at=draft.ends_at,
            timezone=draft.timezone,
            now=now or datetime.now(UTC),
        )
        return self._repository.update(updated)

    def transition(self, task_id: int, status: TaskStatus, *, now: datetime | None = None) -> Task:
        task = self.get(task_id)
        return self._repository.update(task.transition_to(status, now=now or datetime.now(UTC)))

    def get(self, task_id: int) -> Task:
        task = self._repository.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"업무 {task_id}을(를) 찾을 수 없습니다.")
        return task

    def list(self, view: TaskView, *, search: str = "", now: datetime | None = None) -> list[Task]:
        current = now or datetime.now(UTC)
        tasks = self._repository.list(search=search)
        filtered = [task for task in tasks if self._matches_view(task, view, current)]
        return sorted(filtered, key=self._sort_key)

    def summary(self, *, now: datetime | None = None) -> TaskSummary:
        current = now or datetime.now(UTC)
        zone = ZoneInfo(self._timezone)
        local_day = current.astimezone(zone).date()
        day_start = datetime.combine(local_day, time.min, tzinfo=zone).astimezone(UTC)
        day_end = (
            datetime.combine(local_day, time.min, tzinfo=zone) + timedelta(days=1)
        ).astimezone(UTC)
        tasks = self._repository.list()
        active = {TaskStatus.ACTIVE, TaskStatus.PENDING}
        return TaskSummary(
            overdue=sum(
                task.status in active
                and task.starts_at is not None
                and (task.ends_at or task.starts_at) < current
                for task in tasks
            ),
            in_progress=sum(
                task.status in active
                and task.starts_at is not None
                and task.ends_at is not None
                and task.starts_at <= current < task.ends_at
                for task in tasks
            ),
            today=sum(self._overlaps(task, day_start, day_end) for task in tasks),
            completed_today=sum(
                task.completed_at is not None and day_start <= task.completed_at < day_end
                for task in tasks
            ),
        )

    def _matches_view(self, task: Task, view: TaskView, current: datetime) -> bool:
        zone = ZoneInfo(self._timezone)
        day = current.astimezone(zone).date()
        day_start = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
        day_end = (datetime.combine(day, time.min, tzinfo=zone) + timedelta(days=1)).astimezone(UTC)
        if view is TaskView.TODAY:
            return self._overlaps(task, day_start, day_end)
        if view is TaskView.UPCOMING:
            return task.status in {TaskStatus.ACTIVE, TaskStatus.PENDING} and bool(
                task.starts_at and task.starts_at >= day_end
            )
        if view is TaskView.IMPORTANT:
            return task.status in {TaskStatus.ACTIVE, TaskStatus.PENDING} and task.priority in {
                TaskPriority.IMPORTANT,
                TaskPriority.URGENT,
            }
        if view is TaskView.PENDING:
            return task.status is TaskStatus.PENDING
        if view is TaskView.COMPLETED:
            return task.status is TaskStatus.COMPLETED
        return task.status is not TaskStatus.ARCHIVED

    @staticmethod
    def _overlaps(task: Task, start: datetime, end: datetime) -> bool:
        if task.starts_at is None:
            return False
        task_end = task.ends_at or task.starts_at + timedelta(microseconds=1)
        return task.starts_at < end and task_end > start

    @staticmethod
    def _sort_key(task: Task) -> tuple[bool, datetime, int, str]:
        fallback = datetime.max.replace(tzinfo=UTC)
        priority_order = {
            TaskPriority.URGENT: 0,
            TaskPriority.IMPORTANT: 1,
            TaskPriority.ATTENTION: 2,
            TaskPriority.NORMAL: 3,
        }
        return (
            not task.is_pinned,
            task.starts_at or fallback,
            priority_order[task.priority],
            task.title,
        )
