from __future__ import annotations

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
from officeflow.domain.enums import TaskPriority, TaskStatus
from officeflow.domain.task import Task


class InMemoryTaskRepository(TaskRepository):
    def __init__(self) -> None:
        self.tasks: dict[int, Task] = {}
        self.next_id = 1

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

    @staticmethod
    def _matches(
        task: Task,
        query: TaskQuery,
        current: datetime,
        day_start: datetime,
        day_end: datetime,
    ) -> bool:
        active = {TaskStatus.ACTIVE, TaskStatus.PENDING}
        task_end = task.ends_at or task.starts_at
        overlaps_today = bool(
            task.starts_at
            and task.starts_at < day_end
            and (
                (task.ends_at is not None and task.ends_at > day_start)
                or (task.ends_at is None and task.starts_at >= day_start)
            )
        )
        if query.group is TaskGroup.OVERDUE:
            view_match = bool(task.status in active and task_end and task_end <= current)
        elif query.group is TaskGroup.IN_PROGRESS:
            view_match = bool(
                task.status in active
                and task.starts_at
                and task.ends_at
                and task.starts_at <= current < task.ends_at
            )
        elif query.group is TaskGroup.UPCOMING:
            view_match = bool(
                task.status in active and task.starts_at and current <= task.starts_at < day_end
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
