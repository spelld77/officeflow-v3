from __future__ import annotations

import builtins
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

from officeflow.domain.enums import OccurrenceStatus, TaskPriority, TaskStatus
from officeflow.domain.occurrence import TaskOccurrence
from officeflow.domain.recurrence import expand_recurrence, next_recurrence_start
from officeflow.domain.reminder import ReminderRuleInput
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
    recurrence_rule: str | None = None
    reminder_rules: tuple[ReminderRuleInput, ...] = ()


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    task: Task
    starts_at: datetime
    ends_at: datetime | None
    occurrence_start: datetime | None = None
    occurrence_status: OccurrenceStatus | None = None
    completed_at: datetime | None = None

    @property
    def id(self) -> int | None:
        return self.task.id

    @property
    def title(self) -> str:
        return self.task.title

    @property
    def description(self) -> str:
        return self.task.description

    @property
    def priority(self) -> TaskPriority:
        return self.task.priority

    @property
    def is_pinned(self) -> bool:
        return self.task.is_pinned

    @property
    def all_day(self) -> bool:
        return self.task.all_day

    @property
    def timezone(self) -> str:
        return self.task.timezone

    @property
    def status(self) -> TaskStatus:
        return self.display_task.status

    @property
    def display_task(self) -> Task:
        status = self.task.status
        if self.occurrence_status is OccurrenceStatus.COMPLETED:
            status = TaskStatus.COMPLETED
        elif self.occurrence_status is OccurrenceStatus.CANCELED:
            status = TaskStatus.CANCELED
        return replace(
            self.task,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            status=status,
            completed_at=self.completed_at if status is TaskStatus.COMPLETED else None,
        )


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

    def list_overlapping(
        self,
        starts_at: datetime,
        ends_at: datetime,
        *,
        search: str = "",
    ) -> tuple[Task, ...]: ...

    def get_occurrence(self, task_id: int, occurrence_start: datetime) -> TaskOccurrence | None: ...

    def save_occurrence(self, occurrence: TaskOccurrence) -> TaskOccurrence: ...

    def list_occurrences(
        self,
        task_ids: tuple[int, ...],
        starts_at: datetime,
        ends_at: datetime,
    ) -> tuple[TaskOccurrence, ...]: ...


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
            recurrence_rule=draft.recurrence_rule,
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
            recurrence_rule=draft.recurrence_rule,
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
        if query.view is TaskView.TODAY:
            return self._query_today_with_recurrence(
                query,
                current=current,
                day_start=day_start,
                day_end=day_end,
            )
        return self._repository.query(
            query,
            current=current,
            day_start=day_start,
            day_end=day_end,
        )

    def list(self, view: TaskView, *, search: str = "", now: datetime | None = None) -> list[Task]:
        page = self.query(TaskQuery(view=view, search=search, limit=None), now=now)
        return list(page.items)

    def calendar_range(
        self,
        start_date: date,
        end_date: date,
        *,
        search: str = "",
    ) -> tuple[Task, ...]:
        """Return scheduled tasks overlapping [start_date, end_date) in app time."""
        if end_date <= start_date:
            raise ValueError("캘린더 종료일은 시작일보다 늦어야 합니다.")
        zone = ZoneInfo(self._timezone)
        starts_at = datetime.combine(start_date, time.min, tzinfo=zone).astimezone(UTC)
        ends_at = datetime.combine(end_date, time.min, tzinfo=zone).astimezone(UTC)
        return self._repository.list_overlapping(starts_at, ends_at, search=search)

    def calendar_schedule(
        self,
        start_date: date,
        end_date: date,
        *,
        search: str = "",
    ) -> tuple[ScheduledTask, ...]:
        templates = self.calendar_range(start_date, end_date, search=search)
        zone = ZoneInfo(self._timezone)
        range_start = datetime.combine(start_date, time.min, tzinfo=zone).astimezone(UTC)
        range_end = datetime.combine(end_date, time.min, tzinfo=zone).astimezone(UTC)
        recurring_ids = tuple(
            task.id for task in templates if task.recurrence_rule and task.id is not None
        )
        longest_duration = max(
            (
                task.ends_at - task.starts_at
                for task in templates
                if task.recurrence_rule and task.starts_at and task.ends_at
            ),
            default=timedelta(0),
        )
        saved = self._repository.list_occurrences(
            recurring_ids, range_start - longest_duration, range_end
        )
        saved_by_key = {
            (occurrence.task_id, occurrence.occurrence_start): occurrence for occurrence in saved
        }
        scheduled: list[ScheduledTask] = []
        for task in templates:
            if task.starts_at is None:
                continue
            if not task.recurrence_rule or task.id is None:
                scheduled.append(ScheduledTask(task, task.starts_at, task.ends_at))
                continue
            for occurrence_start, occurrence_end in expand_recurrence(
                task.recurrence_rule,
                template_start=task.starts_at,
                template_end=task.ends_at,
                timezone=task.timezone,
                range_start=range_start,
                range_end=range_end,
            ):
                occurrence = saved_by_key.get((task.id, occurrence_start))
                if occurrence is not None and occurrence.status in {
                    OccurrenceStatus.SKIPPED,
                    OccurrenceStatus.CANCELED,
                }:
                    continue
                scheduled.append(
                    ScheduledTask(
                        task=task,
                        starts_at=occurrence.starts_at if occurrence else occurrence_start,
                        ends_at=occurrence.ends_at if occurrence else occurrence_end,
                        occurrence_start=occurrence_start,
                        occurrence_status=(
                            occurrence.status if occurrence else OccurrenceStatus.PENDING
                        ),
                        completed_at=occurrence.completed_at if occurrence else None,
                    )
                )
        return tuple(
            sorted(
                scheduled,
                key=lambda item: (
                    item.starts_at,
                    not item.task.is_pinned,
                    item.task.title.casefold(),
                    item.task.id or 0,
                ),
            )
        )

    def transition_occurrence(
        self,
        task_id: int,
        occurrence_start: datetime,
        status: OccurrenceStatus,
        *,
        now: datetime | None = None,
        result_note: str | None = None,
    ) -> TaskOccurrence:
        task = self.get(task_id)
        if not task.recurrence_rule or task.starts_at is None:
            raise ValueError("반복 업무의 발생 건만 개별 처리할 수 있습니다.")
        expected = next_recurrence_start(
            task.recurrence_rule,
            template_start=task.starts_at,
            timezone=task.timezone,
            after=occurrence_start,
            inclusive=True,
        )
        if expected != occurrence_start:
            raise ValueError("반복 규칙에 포함되지 않은 발생 시각입니다.")
        occurrence = self._repository.get_occurrence(task_id, occurrence_start)
        if occurrence is None:
            duration = task.ends_at - task.starts_at if task.ends_at is not None else None
            occurrence = TaskOccurrence(
                id=None,
                task_id=task_id,
                occurrence_start=occurrence_start,
                occurrence_end=(occurrence_start + duration if duration is not None else None),
            )
        return self._repository.save_occurrence(
            occurrence.transition(
                status,
                now=now or datetime.now(UTC),
                result_note=result_note,
            )
        )

    def next_occurrence(
        self,
        task_id: int,
        *,
        after: datetime | None = None,
    ) -> ScheduledTask | None:
        task = self.get(task_id)
        if not task.recurrence_rule or task.starts_at is None:
            return None
        cursor = after or datetime.now(UTC)
        duration = task.ends_at - task.starts_at if task.ends_at is not None else None
        for _attempt in range(1_000):
            occurrence_start = next_recurrence_start(
                task.recurrence_rule,
                template_start=task.starts_at,
                timezone=task.timezone,
                after=cursor,
                inclusive=False,
            )
            if occurrence_start is None:
                return None
            saved = self._repository.get_occurrence(task_id, occurrence_start)
            if saved is None or saved.status is OccurrenceStatus.PENDING:
                return ScheduledTask(
                    task=task,
                    starts_at=saved.starts_at if saved else occurrence_start,
                    ends_at=(
                        saved.ends_at
                        if saved
                        else occurrence_start + duration
                        if duration is not None
                        else None
                    ),
                    occurrence_start=occurrence_start,
                    occurrence_status=OccurrenceStatus.PENDING,
                )
            cursor = occurrence_start
        raise RuntimeError("다음 반복 일정을 계산할 수 없습니다.")

    def _query_today_with_recurrence(
        self,
        query: TaskQuery,
        *,
        current: datetime,
        day_start: datetime,
        day_end: datetime,
    ) -> TaskPage:
        regular_query = replace(query, offset=0, limit=None)
        regular = self._repository.query(
            regular_query,
            current=current,
            day_start=day_start,
            day_end=day_end,
        )
        zone = ZoneInfo(self._timezone)
        day = day_start.astimezone(zone).date()
        recurring = [
            item.display_task
            for item in self.calendar_schedule(day, day + timedelta(days=1), search=query.search)
            if item.occurrence_start is not None
            and self._matches_recurrence_filters(item.display_task, query)
            and (
                query.group is None
                or self._recurrence_group(item.display_task, current, day_start, day_end)
                is query.group
            )
        ]
        combined = self._sort_tasks([*regular.items, *recurring], query.sort)
        end = None if query.limit is None else query.offset + query.limit
        return TaskPage(
            items=tuple(combined[query.offset : end]),
            total=len(combined),
            offset=query.offset,
            limit=query.limit,
        )

    @staticmethod
    def _matches_recurrence_filters(task: Task, query: TaskQuery) -> bool:
        return (
            (not query.statuses or task.status in query.statuses)
            and (not query.priorities or task.priority in query.priorities)
            and (not query.pinned_only or task.is_pinned)
        )

    @staticmethod
    def _recurrence_group(
        task: Task,
        current: datetime,
        day_start: datetime,
        day_end: datetime,
    ) -> TaskGroup | None:
        if (
            task.status is TaskStatus.COMPLETED
            and task.completed_at is not None
            and day_start <= task.completed_at < day_end
        ):
            return TaskGroup.COMPLETED
        if task.status not in {TaskStatus.ACTIVE, TaskStatus.PENDING} or task.starts_at is None:
            return None
        if task.ends_at is not None and task.ends_at <= current:
            return TaskGroup.OVERDUE
        if task.ends_at is None and task.starts_at < current:
            return TaskGroup.OVERDUE
        if task.ends_at is not None and task.starts_at <= current < task.ends_at:
            return TaskGroup.IN_PROGRESS
        if task.starts_at >= current and task.starts_at < day_end:
            return TaskGroup.UPCOMING
        return None

    @staticmethod
    def _sort_tasks(tasks: builtins.list[Task], sort: TaskSort) -> builtins.list[Task]:
        priority = {
            TaskPriority.URGENT: 0,
            TaskPriority.IMPORTANT: 1,
            TaskPriority.ATTENTION: 2,
            TaskPriority.NORMAL: 3,
        }
        fallback = datetime.max.replace(tzinfo=UTC)
        if sort is TaskSort.PRIORITY:
            return sorted(
                tasks,
                key=lambda task: (
                    not task.is_pinned,
                    priority[task.priority],
                    task.starts_at or fallback,
                ),
            )
        if sort is TaskSort.UPDATED:
            return sorted(
                tasks,
                key=lambda task: (not task.is_pinned, -task.updated_at.timestamp()),
            )
        if sort is TaskSort.TITLE:
            return sorted(tasks, key=lambda task: (not task.is_pinned, task.title.casefold()))
        return sorted(
            tasks,
            key=lambda task: (
                not task.is_pinned,
                task.starts_at or fallback,
                priority[task.priority],
                task.title,
            ),
        )

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
