from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from officeflow.application.reminders import ReminderService
from officeflow.application.tasks import (
    TaskDraft,
    TaskGroup,
    TaskQuery,
    TaskService,
    TaskSort,
    TaskView,
)
from officeflow.domain.enums import OccurrenceStatus, ReminderRelation, TaskPriority, TaskStatus
from officeflow.domain.reminder import ReminderRuleInput
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.reminder_repository import SqlAlchemyReminderRepository
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository


def test_repository_round_trip_and_search(tmp_path: Path) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    service = TaskService(SqlAlchemyTaskRepository(SessionFactory(engine)))
    start = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)

    saved = service.create(
        TaskDraft(
            title="현장 점검",
            description="A동부터 C동까지 확인",
            priority=TaskPriority.IMPORTANT,
            all_day=True,
            starts_at=start,
            ends_at=start + timedelta(days=4),
        ),
        now=start,
    )

    assert saved.id is not None
    restored = service.get(saved.id)
    assert restored.starts_at == start
    assert restored.starts_at.tzinfo is UTC
    assert [task.id for task in service.list(TaskView.ALL, search="C동", now=start)] == [saved.id]

    updated = service.update(
        saved.id,
        TaskDraft(
            title="현장 점검 수정",
            description=restored.description,
            priority=TaskPriority.URGENT,
            all_day=True,
            starts_at=restored.starts_at,
            ends_at=restored.ends_at,
        ),
        now=start + timedelta(minutes=30),
    )
    assert updated.title == "현장 점검 수정"
    assert updated.priority is TaskPriority.URGENT

    completed = service.transition(saved.id, TaskStatus.COMPLETED, now=start + timedelta(hours=1))
    assert completed.completed_at == start + timedelta(hours=1)
    engine.dispose()


def test_repository_queries_groups_filters_and_pages(tmp_path: Path) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    service = TaskService(SqlAlchemyTaskRepository(SessionFactory(engine)))
    now = datetime(2026, 9, 12, 3, 0, tzinfo=UTC)
    tasks = (
        TaskDraft(
            title="지난 지연 업무",
            priority=TaskPriority.URGENT,
            starts_at=now - timedelta(days=3),
            ends_at=now - timedelta(days=2),
        ),
        TaskDraft(
            title="현재 긴급 업무",
            priority=TaskPriority.URGENT,
            is_pinned=True,
            starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(hours=1),
        ),
        TaskDraft(
            title="오후 중요 업무",
            priority=TaskPriority.IMPORTANT,
            starts_at=now + timedelta(hours=2),
            ends_at=now + timedelta(hours=3),
        ),
        TaskDraft(
            title="완료 업무",
            status=TaskStatus.COMPLETED,
            starts_at=now - timedelta(hours=2),
            ends_at=now - timedelta(hours=1),
        ),
        TaskDraft(title="현재 시점 업무", starts_at=now),
    )
    for task in tasks:
        service.create(task, now=now)

    page = service.query(
        TaskQuery(
            search="업무",
            priorities=frozenset({TaskPriority.URGENT}),
            sort=TaskSort.PRIORITY,
            limit=1,
        ),
        now=now,
    )
    groups = service.today_groups(now=now)

    assert page.total == 2
    assert page.has_more is True
    assert [task.title for task in page.items] == ["현재 긴급 업무"]
    assert groups[TaskGroup.OVERDUE].total == 1
    assert groups[TaskGroup.IN_PROGRESS].total == 1
    assert groups[TaskGroup.UPCOMING].total == 2
    assert groups[TaskGroup.COMPLETED].total == 1
    engine.dispose()


def test_recurrence_occurrence_round_trip_does_not_complete_template(tmp_path: Path) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    service = TaskService(SqlAlchemyTaskRepository(SessionFactory(engine)))
    start = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
    task = service.create(
        TaskDraft(
            title="매일 점검",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            recurrence_rule="FREQ=DAILY;INTERVAL=1;UNTIL=20260920T235959Z",
        ),
        now=start,
    )
    assert task.id is not None

    completed = service.transition_occurrence(
        task.id,
        start + timedelta(days=1),
        OccurrenceStatus.COMPLETED,
        now=start + timedelta(days=1, hours=1),
    )
    restored = service.calendar_schedule(start.date(), (start + timedelta(days=4)).date())

    assert completed.id is not None
    assert completed.status is OccurrenceStatus.COMPLETED
    assert service.get(task.id).status is TaskStatus.ACTIVE
    assert [item.occurrence_status for item in restored] == [
        OccurrenceStatus.PENDING,
        OccurrenceStatus.COMPLETED,
        OccurrenceStatus.PENDING,
        OccurrenceStatus.PENDING,
    ]
    engine.dispose()


def test_reminder_delivery_history_prevents_duplicate_after_repository_restart(
    tmp_path: Path,
) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    sessions = SessionFactory(engine)
    task_service = TaskService(SqlAlchemyTaskRepository(sessions))
    reminder_service = ReminderService(SqlAlchemyReminderRepository(sessions), task_service)
    start = datetime(2026, 9, 14, 1, 0, tzinfo=UTC)
    task = task_service.create(TaskDraft(title="재시작 확인", starts_at=start), now=start)
    assert task.id is not None
    reminder_service.replace_rules(
        task.id,
        (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),),
    )

    first = reminder_service.poll_due(now=start)
    restarted = ReminderService(SqlAlchemyReminderRepository(sessions), task_service)
    duplicate = restarted.poll_due(now=start + timedelta(minutes=1))
    assert first[0].delivery.id is not None
    snoozed = restarted.snooze(
        first[0].delivery.id,
        10,
        now=start + timedelta(minutes=1),
    )
    refired = restarted.poll_due(now=start + timedelta(minutes=11))

    assert duplicate == ()
    assert snoozed.snoozed_until == start + timedelta(minutes=11)
    assert len(refired) == 1
    assert refired[0].delivery.id == first[0].delivery.id
    engine.dispose()
