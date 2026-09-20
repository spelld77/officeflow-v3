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
from officeflow.infrastructure.database.models import AttachmentRecord, ReminderRecord
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


def test_repository_soft_delete_and_restore_round_trip(tmp_path: Path) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    sessions = SessionFactory(engine)
    service = TaskService(SqlAlchemyTaskRepository(sessions))
    created_at = datetime(2026, 9, 19, 1, 0, tzinfo=UTC)
    task = service.create(
        TaskDraft(title="휴지통 보존", description="기록과 첨부는 지우지 않음"),
        now=created_at,
    )
    assert task.id is not None
    with sessions.transaction() as session:
        session.add(
            AttachmentRecord(
                task_id=task.id,
                original_name="evidence.txt",
                stored_name="preserved-evidence.txt",
                relative_path="preserved-evidence.txt",
                size_bytes=12,
                checksum=None,
                created_at=created_at,
                missing_at=None,
            )
        )

    service.move_to_trash(task.id, now=created_at + timedelta(minutes=1))

    assert service.list(TaskView.ALL, now=created_at) == []
    trash = service.list(TaskView.TRASH, now=created_at)
    assert [item.id for item in trash] == [task.id]
    assert trash[0].deleted_at == created_at + timedelta(minutes=1)
    assert trash[0].has_attachments is True

    restored = service.restore_from_trash(
        task.id,
        now=created_at + timedelta(minutes=2),
    )

    assert restored.description == "기록과 첨부는 지우지 않음"
    assert restored.deleted_at is None
    assert restored.has_attachments is True
    assert service.list(TaskView.TRASH, now=created_at) == []
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


def test_calendar_overview_limits_rows_but_keeps_day_count(tmp_path: Path) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    service = TaskService(SqlAlchemyTaskRepository(SessionFactory(engine)))
    local_day_start = datetime(2026, 9, 16, 15, 0, tzinfo=UTC)
    for index in range(130):
        service.create(
            TaskDraft(
                title=f"밀집 일정 {index:03d}",
                starts_at=local_day_start,
                ends_at=local_day_start + timedelta(hours=1),
            ),
            now=local_day_start,
        )

    overview = service.calendar_overview(
        local_day_start.date(),
        (local_day_start + timedelta(days=3)).date(),
    )

    assert overview.total == 130
    assert overview.count_for(datetime(2026, 9, 17, tzinfo=UTC).date()) == 130
    assert overview.summary_mode is True
    assert overview.preview_tasks == ()

    filtered = service.calendar_overview(
        local_day_start.date(),
        (local_day_start + timedelta(days=3)).date(),
        search="밀집 001",
    )

    assert filtered.total == 1
    assert filtered.count_for(datetime(2026, 9, 17, tzinfo=UTC).date()) == 1
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
    assert restarted.active_snoozes() == (snoozed,)
    refired = restarted.poll_due(now=start + timedelta(minutes=11))

    assert duplicate == ()
    assert snoozed.snoozed_until == start + timedelta(minutes=11)
    assert len(refired) == 1
    assert refired[0].delivery.id == first[0].delivery.id
    assert restarted.active_snoozes() == ()
    engine.dispose()


def test_task_schedule_change_invalidates_and_rebuilds_reminder_cache(
    tmp_path: Path,
) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    sessions = SessionFactory(engine)
    task_service = TaskService(SqlAlchemyTaskRepository(sessions))
    reminder_service = ReminderService(SqlAlchemyReminderRepository(sessions), task_service)
    first_due = datetime(2026, 9, 20, 1, 0, tzinfo=UTC)
    task = task_service.create(TaskDraft(title="일정 변경", starts_at=first_due))
    assert task.id is not None
    reminder = reminder_service.replace_rules(
        task.id,
        (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),),
    )[0]
    assert reminder.id is not None
    with sessions.transaction() as session:
        cached = session.get(ReminderRecord, reminder.id)
        assert cached is not None
        assert cached.schedule_initialized is True
        assert cached.next_fire_at == first_due

    changed_due = first_due + timedelta(hours=2)
    task_service.update(
        task.id,
        TaskDraft(title=task.title, starts_at=changed_due),
        now=first_due - timedelta(hours=1),
    )
    with sessions.transaction() as session:
        invalidated = session.get(ReminderRecord, reminder.id)
        assert invalidated is not None
        assert invalidated.schedule_initialized is False
        assert invalidated.next_fire_at is None

    alerts = reminder_service.poll_due(now=changed_due)

    assert len(alerts) == 1
    assert alerts[0].delivery.scheduled_at == changed_due
    engine.dispose()


def test_soft_deleted_task_does_not_fire_reminders(tmp_path: Path) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    sessions = SessionFactory(engine)
    task_service = TaskService(SqlAlchemyTaskRepository(sessions))
    reminder_service = ReminderService(SqlAlchemyReminderRepository(sessions), task_service)
    due_at = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
    task = task_service.create(
        TaskDraft(title="삭제한 업무 알림", starts_at=due_at),
        now=due_at - timedelta(hours=1),
    )
    assert task.id is not None
    reminder_service.replace_rules(
        task.id,
        (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),),
    )

    task_service.move_to_trash(task.id, now=due_at - timedelta(minutes=1))

    assert reminder_service.poll_due(now=due_at) == ()
    engine.dispose()
