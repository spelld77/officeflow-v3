from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from officeflow.application.tasks import (
    TaskDraft,
    TaskGroup,
    TaskPage,
    TaskQuery,
    TaskRepository,
    TaskService,
    TaskSort,
    TaskView,
)
from officeflow.domain.enums import OccurrenceStatus, TaskPriority, TaskStatus
from officeflow.domain.occurrence import TaskOccurrence
from officeflow.domain.task import Task


class InMemoryTaskRepository(TaskRepository):
    def __init__(self) -> None:
        self.tasks: dict[int, Task] = {}
        self.occurrences: dict[tuple[int, datetime], TaskOccurrence] = {}
        self.next_id = 1
        self.next_occurrence_id = 1

    def add(self, task: Task) -> Task:
        saved = Task(
            id=self.next_id,
            title=task.title,
            description=task.description,
            status=task.status,
            priority=task.priority,
            is_pinned=task.is_pinned,
            all_day=task.all_day,
            starts_at=task.starts_at,
            ends_at=task.ends_at,
            timezone=task.timezone,
            recurrence_rule=task.recurrence_rule,
            result_note=task.result_note,
            completed_at=task.completed_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
        self.tasks[self.next_id] = saved
        self.next_id += 1
        return saved

    def update(self, task: Task) -> Task:
        assert task.id is not None
        self.tasks[task.id] = task
        return task

    def get(self, task_id: int) -> Task | None:
        return self.tasks.get(task_id)

    def query(
        self,
        query: TaskQuery,
        *,
        current: datetime,
        day_start: datetime,
        day_end: datetime,
    ) -> TaskPage:
        tasks = [
            task
            for task in self.tasks.values()
            if self._matches(task, query, current, day_start, day_end)
        ]
        tasks = self._sort(tasks, query.sort)
        total = len(tasks)
        end = None if query.limit is None else query.offset + query.limit
        return TaskPage(tuple(tasks[query.offset : end]), total, query.offset, query.limit)

    def list_overlapping(
        self,
        starts_at: datetime,
        ends_at: datetime,
        *,
        search: str = "",
    ) -> tuple[Task, ...]:
        normalized = search.strip().casefold()
        return tuple(
            task
            for task in self.tasks.values()
            if task.status is not TaskStatus.ARCHIVED
            and task.starts_at is not None
            and task.starts_at < ends_at
            and (
                task.recurrence_rule is not None
                or (task.ends_at is not None and task.ends_at > starts_at)
                or (task.ends_at is None and task.starts_at >= starts_at)
            )
            and (not normalized or normalized in f"{task.title}\n{task.description}".casefold())
        )

    def get_occurrence(self, task_id: int, occurrence_start: datetime) -> TaskOccurrence | None:
        return self.occurrences.get((task_id, occurrence_start))

    def save_occurrence(self, occurrence: TaskOccurrence) -> TaskOccurrence:
        saved = replace(occurrence, id=occurrence.id or self.next_occurrence_id)
        if occurrence.id is None:
            self.next_occurrence_id += 1
        self.occurrences[(occurrence.task_id, occurrence.occurrence_start)] = saved
        return saved

    def list_occurrences(
        self,
        task_ids: tuple[int, ...],
        starts_at: datetime,
        ends_at: datetime,
    ) -> tuple[TaskOccurrence, ...]:
        return tuple(
            occurrence
            for occurrence in self.occurrences.values()
            if occurrence.task_id in task_ids and starts_at <= occurrence.occurrence_start < ends_at
        )

    @staticmethod
    def _matches(
        task: Task,
        query: TaskQuery,
        current: datetime,
        day_start: datetime,
        day_end: datetime,
    ) -> bool:
        if (query.view is TaskView.TODAY or query.group is not None) and task.recurrence_rule:
            return False
        active = {TaskStatus.ACTIVE, TaskStatus.PENDING}
        overlaps_today = bool(
            task.starts_at
            and task.starts_at < day_end
            and (
                (task.ends_at is not None and task.ends_at > day_start)
                or (task.ends_at is None and task.starts_at >= day_start)
            )
        )
        if query.group is TaskGroup.OVERDUE:
            view_match = bool(
                task.status in active
                and task.starts_at
                and (
                    (task.ends_at is not None and task.ends_at <= current)
                    or (task.ends_at is None and task.starts_at < current)
                )
            )
        elif query.group is TaskGroup.IN_PROGRESS:
            view_match = bool(
                task.status in active
                and task.starts_at
                and task.ends_at
                and task.starts_at <= current < task.ends_at
            )
        elif query.group is TaskGroup.UPCOMING:
            view_match = bool(
                task.status in active
                and task.starts_at
                and (
                    task.starts_at > current or (task.starts_at == current and task.ends_at is None)
                )
                and task.starts_at < day_end
            )
        elif query.group is TaskGroup.COMPLETED:
            view_match = bool(
                task.status is TaskStatus.COMPLETED
                and task.completed_at
                and day_start <= task.completed_at < day_end
            )
        elif query.view is TaskView.TODAY:
            view_match = task.status is not TaskStatus.ARCHIVED and overlaps_today
        elif query.view is TaskView.UPCOMING:
            view_match = bool(
                task.status in active and task.starts_at and task.starts_at >= day_end
            )
        elif query.view is TaskView.IMPORTANT:
            view_match = task.status in active and task.priority in {
                TaskPriority.IMPORTANT,
                TaskPriority.URGENT,
            }
        elif query.view is TaskView.PENDING:
            view_match = task.status is TaskStatus.PENDING
        elif query.view is TaskView.COMPLETED:
            view_match = task.status is TaskStatus.COMPLETED
        else:
            view_match = task.status is not TaskStatus.ARCHIVED
        normalized = query.search.casefold().strip()
        return (
            view_match
            and (not normalized or normalized in f"{task.title}\n{task.description}".casefold())
            and (not query.statuses or task.status in query.statuses)
            and (not query.priorities or task.priority in query.priorities)
            and (not query.pinned_only or task.is_pinned)
            and (
                query.has_attachments is None
                or task.has_attachments is query.has_attachments
            )
        )

    @staticmethod
    def _sort(tasks: list[Task], sort: TaskSort) -> list[Task]:
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
                tasks, key=lambda task: (not task.is_pinned, -task.updated_at.timestamp())
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


NOW = datetime(2026, 9, 12, 3, 0, tzinfo=UTC)


def test_quick_add_from_today_creates_inclusive_local_day() -> None:
    repository = InMemoryTaskRepository()
    service = TaskService(repository, timezone="Asia/Seoul")

    task = service.quick_add(
        "오늘 할 업무", view=TaskView.TODAY, local_date=date(2026, 9, 12), now=NOW
    )

    assert task.all_day is True
    assert task.starts_at == datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
    assert task.ends_at == datetime(2026, 9, 12, 15, 0, tzinfo=UTC)


def test_multiday_task_appears_on_every_overlapping_day() -> None:
    repository = InMemoryTaskRepository()
    service = TaskService(repository, timezone="Asia/Seoul")
    service.create(
        TaskDraft(
            title="현장 점검",
            all_day=True,
            starts_at=datetime(2026, 9, 13, 15, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 17, 15, 0, tzinfo=UTC),
        ),
        now=NOW,
    )

    for local_day in range(14, 18):
        local_now = datetime(2026, 9, local_day, 3, 0, tzinfo=UTC)
        assert len(service.list(TaskView.TODAY, now=local_now)) == 1

    assert service.list(TaskView.TODAY, now=datetime(2026, 9, 18, 3, 0, tzinfo=UTC)) == []


def test_calendar_range_includes_every_overlap_and_excludes_archived() -> None:
    repository = InMemoryTaskRepository()
    service = TaskService(repository, timezone="Asia/Seoul")
    crossing = service.create(
        TaskDraft(
            title="월 경계 출장",
            all_day=True,
            starts_at=datetime(2026, 8, 30, 15, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 2, 15, 0, tzinfo=UTC),
        ),
        now=NOW,
    )
    point = service.create(
        TaskDraft(title="9월 회의", starts_at=datetime(2026, 9, 15, 1, 0, tzinfo=UTC)),
        now=NOW,
    )
    archived = service.create(
        TaskDraft(title="보관 일정", starts_at=datetime(2026, 9, 16, 1, 0, tzinfo=UTC)),
        now=NOW,
    )
    assert archived.id is not None
    service.transition(archived.id, TaskStatus.ARCHIVED, now=NOW)

    tasks = service.calendar_range(date(2026, 9, 1), date(2026, 10, 1))

    assert {task.id for task in tasks} == {crossing.id, point.id}
    assert [
        task.id
        for task in service.calendar_range(date(2026, 9, 1), date(2026, 10, 1), search="회의")
    ] == [point.id]


def test_calendar_range_rejects_empty_or_reversed_range() -> None:
    service = TaskService(InMemoryTaskRepository())

    try:
        service.calendar_range(date(2026, 9, 1), date(2026, 9, 1))
    except ValueError as error:
        assert "종료일" in str(error)
    else:
        raise AssertionError("empty calendar ranges must be rejected")


def test_completing_one_recurrence_keeps_future_occurrences() -> None:
    repository = InMemoryTaskRepository()
    service = TaskService(repository, timezone="Asia/Seoul")
    task = service.create(
        TaskDraft(
            title="매일 점검",
            all_day=True,
            starts_at=datetime(2026, 9, 12, 15, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 13, 15, 0, tzinfo=UTC),
            recurrence_rule="FREQ=DAILY;INTERVAL=1",
        ),
        now=NOW,
    )
    assert task.id is not None
    completed_start = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)

    occurrence = service.transition_occurrence(
        task.id,
        completed_start,
        OccurrenceStatus.COMPLETED,
        now=NOW,
        result_note="점검 완료",
    )
    scheduled = service.calendar_schedule(date(2026, 9, 13), date(2026, 9, 17))

    assert occurrence.completed_at == NOW
    assert occurrence.result_note == "점검 완료"
    assert len(scheduled) == 4
    assert scheduled[1].occurrence_status is OccurrenceStatus.COMPLETED
    assert scheduled[2].occurrence_status is OccurrenceStatus.PENDING
    assert service.get(task.id).status is TaskStatus.ACTIVE


def test_skipped_recurrence_is_hidden_and_next_ignores_completed() -> None:
    repository = InMemoryTaskRepository()
    service = TaskService(repository, timezone="Asia/Seoul")
    task = service.create(
        TaskDraft(
            title="주간 보고",
            starts_at=datetime(2026, 9, 7, 0, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 7, 1, 0, tzinfo=UTC),
            recurrence_rule="FREQ=WEEKLY;INTERVAL=1",
        ),
        now=NOW,
    )
    assert task.id is not None
    skipped_start = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
    service.transition_occurrence(task.id, skipped_start, OccurrenceStatus.SKIPPED, now=NOW)

    scheduled = service.calendar_schedule(date(2026, 9, 14), date(2026, 9, 29))
    next_item = service.next_occurrence(task.id, after=datetime(2026, 9, 13, 0, 0, tzinfo=UTC))

    assert [item.occurrence_start for item in scheduled] == [
        datetime(2026, 9, 21, 0, 0, tzinfo=UTC),
        datetime(2026, 9, 28, 0, 0, tzinfo=UTC),
    ]
    assert next_item is not None
    assert next_item.occurrence_start == datetime(2026, 9, 21, 0, 0, tzinfo=UTC)


def test_today_groups_include_current_recurrence_and_its_completion() -> None:
    repository = InMemoryTaskRepository()
    service = TaskService(repository, timezone="Asia/Seoul")
    task = service.create(
        TaskDraft(
            title="매일 아침 확인",
            all_day=True,
            starts_at=datetime(2026, 9, 8, 15, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 9, 15, 0, tzinfo=UTC),
            recurrence_rule="FREQ=DAILY;INTERVAL=1",
        ),
        now=NOW - timedelta(days=4),
    )
    assert task.id is not None

    before = service.today_groups(now=NOW)
    service.transition_occurrence(
        task.id,
        datetime(2026, 9, 11, 15, 0, tzinfo=UTC),
        OccurrenceStatus.COMPLETED,
        now=NOW,
    )
    after = service.today_groups(now=NOW)

    assert [item.title for item in before[TaskGroup.IN_PROGRESS].items] == ["매일 아침 확인"]
    assert all(
        item.title != "매일 아침 확인"
        for group, page in after.items()
        if group is not TaskGroup.COMPLETED
        for item in page.items
    )
    assert [item.title for item in after[TaskGroup.COMPLETED].items] == ["매일 아침 확인"]


def test_views_and_search_filter_tasks() -> None:
    repository = InMemoryTaskRepository()
    service = TaskService(repository)
    service.create(TaskDraft(title="중요 계약 검토", priority=TaskPriority.IMPORTANT), now=NOW)
    pending = service.create(TaskDraft(title="자료 대기"), now=NOW)
    assert pending.id is not None
    service.transition(pending.id, TaskStatus.PENDING, now=NOW + timedelta(minutes=1))

    assert [task.title for task in service.list(TaskView.IMPORTANT, now=NOW)] == ["중요 계약 검토"]
    assert [task.title for task in service.list(TaskView.PENDING, now=NOW)] == ["자료 대기"]
    assert [task.title for task in service.list(TaskView.ALL, search="계약", now=NOW)] == [
        "중요 계약 검토"
    ]

    service.transition(pending.id, TaskStatus.ARCHIVED, now=NOW + timedelta(minutes=2))
    assert service.list(TaskView.PENDING, now=NOW) == []
    assert all(task.id != pending.id for task in service.list(TaskView.ALL, now=NOW))


def test_summary_counts_time_based_states() -> None:
    repository = InMemoryTaskRepository()
    service = TaskService(repository)
    service.create(
        TaskDraft(
            title="지연 업무",
            starts_at=NOW - timedelta(hours=2),
            ends_at=NOW - timedelta(hours=1),
        ),
        now=NOW - timedelta(days=1),
    )
    service.create(
        TaskDraft(
            title="진행 중 업무",
            starts_at=NOW - timedelta(hours=1),
            ends_at=NOW + timedelta(hours=1),
        ),
        now=NOW - timedelta(days=1),
    )
    service.create(
        TaskDraft(
            title="완료 업무",
            status=TaskStatus.COMPLETED,
            starts_at=NOW - timedelta(hours=1),
            ends_at=NOW + timedelta(hours=1),
        ),
        now=NOW,
    )

    summary = service.summary(now=NOW)

    assert summary.overdue == 1
    assert summary.in_progress == 1
    assert summary.upcoming == 0
    assert summary.today == 3
    assert summary.completed_today == 1


def test_today_groups_are_disjoint_and_include_past_overdue_tasks() -> None:
    repository = InMemoryTaskRepository()
    service = TaskService(repository)
    service.create(
        TaskDraft(
            title="지난주 지연 업무",
            starts_at=NOW - timedelta(days=8),
            ends_at=NOW - timedelta(days=7),
        ),
        now=NOW - timedelta(days=9),
    )
    service.create(
        TaskDraft(
            title="진행 중 업무",
            starts_at=NOW - timedelta(hours=1),
            ends_at=NOW + timedelta(hours=1),
        ),
        now=NOW,
    )
    service.create(
        TaskDraft(
            title="오늘 예정 업무",
            starts_at=NOW + timedelta(hours=2),
            ends_at=NOW + timedelta(hours=3),
        ),
        now=NOW,
    )
    service.create(
        TaskDraft(
            title="오늘 완료 업무",
            status=TaskStatus.COMPLETED,
            starts_at=NOW - timedelta(hours=1),
            ends_at=NOW + timedelta(hours=1),
        ),
        now=NOW,
    )

    groups = service.today_groups(now=NOW)

    assert [task.title for task in groups[TaskGroup.OVERDUE].items] == ["지난주 지연 업무"]
    assert [task.title for task in groups[TaskGroup.IN_PROGRESS].items] == ["진행 중 업무"]
    assert [task.title for task in groups[TaskGroup.UPCOMING].items] == ["오늘 예정 업무"]
    assert [task.title for task in groups[TaskGroup.COMPLETED].items] == ["오늘 완료 업무"]


def test_today_groups_do_not_duplicate_exact_start_boundary() -> None:
    repository = InMemoryTaskRepository()
    service = TaskService(repository)
    service.create(
        TaskDraft(
            title="지금 시작한 기간 업무",
            starts_at=NOW,
            ends_at=NOW + timedelta(hours=1),
        ),
        now=NOW,
    )
    service.create(TaskDraft(title="현재 시점 업무", starts_at=NOW), now=NOW)

    groups = service.today_groups(now=NOW)
    grouped_ids = [task.id for page in groups.values() for task in page.items]

    assert len(grouped_ids) == len(set(grouped_ids)) == 2
    assert [task.title for task in groups[TaskGroup.IN_PROGRESS].items] == ["지금 시작한 기간 업무"]
    assert [task.title for task in groups[TaskGroup.UPCOMING].items] == ["현재 시점 업무"]


def test_query_combines_filters_sorting_and_pagination() -> None:
    repository = InMemoryTaskRepository()
    service = TaskService(repository)
    for index in range(4):
        service.create(
            TaskDraft(
                title=f"검토 업무 {index}",
                priority=TaskPriority.URGENT if index < 3 else TaskPriority.NORMAL,
                is_pinned=index == 2,
            ),
            now=NOW + timedelta(minutes=index),
        )

    page = service.query(
        TaskQuery(
            search="검토",
            priorities=frozenset({TaskPriority.URGENT}),
            sort=TaskSort.TITLE,
            offset=1,
            limit=1,
        ),
        now=NOW,
    )

    assert page.total == 3
    assert page.has_more is True
    assert [task.title for task in page.items] == ["검토 업무 0"]
