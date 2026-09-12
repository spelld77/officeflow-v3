from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from officeflow.application.tasks import TaskDraft, TaskRepository, TaskService, TaskView
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

    def list(self, *, search: str = "") -> list[Task]:
        normalized = search.casefold().strip()
        return [
            task
            for task in self.tasks.values()
            if not normalized
            or normalized in task.title.casefold()
            or normalized in task.description.casefold()
        ]


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
