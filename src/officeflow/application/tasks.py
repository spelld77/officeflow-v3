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


class TaskGroup(StrEnum):
    OVERDUE = "overdue"
    IN_PROGRESS = "in_progress"
    UPCOMING = "upcoming"
    COMPLETED = "completed"


class TaskSort(StrEnum):
    SCHEDULE = "schedule"
    PRIORITY = "priority"
    UPDATED = "updated"
    TITLE = "title"


@dataclass(frozen=True, slots=True)
class TaskQuery:
    view: TaskView = TaskView.ALL
    search: str = ""
    statuses: frozenset[TaskStatus] = frozenset()
    priorities: frozenset[TaskPriority] = frozenset()
    pinned_only: bool = False
    group: TaskGroup | None = None
    sort: TaskSort = TaskSort.SCHEDULE
    offset: int = 0
    limit: int | None = 100

    def __post_init__(self) -> None:
        if self.offset < 0:
            raise ValueError("조회 시작 위치는 0 이상이어야 합니다.")
        if self.limit is not None and not 1 <= self.limit <= 500:
            raise ValueError("한 번에 조회할 업무는 1~500개여야 합니다.")
        if self.group is not None and self.view is not TaskView.TODAY:
            raise ValueError("업무 그룹은 오늘 보기에서만 사용할 수 있습니다.")


@dataclass(frozen=True, slots=True)
class TaskPage:
    items: tuple[Task, ...]
    total: int
    offset: int
    limit: int | None

    @property
    def has_more(self) -> bool:
        return self.limit is not None and self.offset + len(self.items) < self.total


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
    upcoming: int
    today: int
    completed_today: int


class TaskRepository(Protocol):
    def add(self, task: Task) -> Task: ...

    def update(self, task: Task) -> Task: ...

    def get(self, task_id: int) -> Task | None: ...

    def query(
        self,
        query: TaskQuery,
        *,
        current: datetime,
        day_start: datetime,
        day_end: datetime,
    ) -> TaskPage: ...


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

    def query(self, query: TaskQuery, *, now: datetime | None = None) -> TaskPage:
        current, day_start, day_end = self._time_context(now)
        return self._repository.query(
            query,
            current=current,
            day_start=day_start,
            day_end=day_end,
        )

    def list(self, view: TaskView, *, search: str = "", now: datetime | None = None) -> list[Task]:
        page = self.query(TaskQuery(view=view, search=search, limit=None), now=now)
        return list(page.items)

    def today_groups(
        self,
        *,
        search: str = "",
        statuses: frozenset[TaskStatus] = frozenset(),
        priorities: frozenset[TaskPriority] = frozenset(),
        pinned_only: bool = False,
        sort: TaskSort = TaskSort.SCHEDULE,
        limit_per_group: int = 100,
        now: datetime | None = None,
    ) -> dict[TaskGroup, TaskPage]:
        return {
            group: self.query(
                TaskQuery(
                    view=TaskView.TODAY,
                    search=search,
                    statuses=statuses,
                    priorities=priorities,
                    pinned_only=pinned_only,
                    group=group,
                    sort=sort,
                    limit=limit_per_group,
                ),
                now=now,
            )
            for group in TaskGroup
        }

    def summary(self, *, now: datetime | None = None) -> TaskSummary:
        groups = self.today_groups(limit_per_group=1, now=now)
        today = self.query(TaskQuery(view=TaskView.TODAY, limit=1), now=now)
        return TaskSummary(
            overdue=groups[TaskGroup.OVERDUE].total,
            in_progress=groups[TaskGroup.IN_PROGRESS].total,
            upcoming=groups[TaskGroup.UPCOMING].total,
            today=today.total,
            completed_today=groups[TaskGroup.COMPLETED].total,
        )

    def _time_context(self, now: datetime | None) -> tuple[datetime, datetime, datetime]:
        current = now or datetime.now(UTC)
        zone = ZoneInfo(self._timezone)
        day = current.astimezone(zone).date()
        day_start = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
        day_end = (datetime.combine(day, time.min, tzinfo=zone) + timedelta(days=1)).astimezone(UTC)
        return current, day_start, day_end
