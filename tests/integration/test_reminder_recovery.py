from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from officeflow.application.reminders import ReminderService
from officeflow.application.tasks import TaskDraft, TaskService
from officeflow.domain.enums import ReminderRelation, TaskStatus
from officeflow.domain.reminder import ReminderRuleInput
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.reminder_repository import SqlAlchemyReminderRepository
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository


@contextmanager
def _services(
    database: Path,
) -> Iterator[tuple[TaskService, ReminderService, SqlAlchemyReminderRepository]]:
    engine = create_database_engine(database)
    sessions = SessionFactory(engine)
    tasks = TaskService(SqlAlchemyTaskRepository(sessions))
    repository = SqlAlchemyReminderRepository(sessions)
    try:
        yield tasks, ReminderService(repository, tasks), repository
    finally:
        engine.dispose()


def test_fired_delivery_survives_closed_database_and_is_recovered_once(tmp_path: Path) -> None:
    database = tmp_path / "officeflow.db"
    upgrade_database(database)
    now = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)
    with _services(database) as (tasks, reminders, _repository):
        task = tasks.create(TaskDraft(title="DB를 다시 열어 복구", starts_at=now), now=now)
        assert task.id is not None
        reminders.replace_rules(
            task.id, (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),), now=now
        )
        original = reminders.poll_due(now=now)[0]

    with _services(database) as (_tasks, restarted, repository):
        recovered = restarted.poll_due(now=now + timedelta(minutes=1), recover_unhandled=True)

        assert len(recovered) == 1
        assert recovered[0].delivery == original.delivery
        assert recovered[0].recovered is True
        assert restarted.poll_due(now=now + timedelta(minutes=2)) == ()
        assert original.delivery.id is not None
        restarted.acknowledge(original.delivery.id, now=now + timedelta(minutes=3))
        assert repository.get_reminder_delivery(original.delivery.id) is not None

    with _services(database) as (_tasks, restarted_again, _repository):
        assert restarted_again.poll_due(
            now=now + timedelta(minutes=4), recover_unhandled=True
        ) == ()


@pytest.mark.parametrize(
    "action",
    [
        "acknowledge", "complete", "defer", "snooze", "disable", "remove_rule",
        "task_completed", "task_canceled", "task_archived", "trash", "delete",
    ],
)
def test_startup_filters_handled_disabled_or_inactive_deliveries(
    tmp_path: Path, action: str
) -> None:
    database = tmp_path / "officeflow.db"
    upgrade_database(database)
    now = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)
    with _services(database) as (tasks, reminders, _repository):
        task = tasks.create(TaskDraft(title="복구 제외 조건", starts_at=now), now=now)
        assert task.id is not None
        reminders.replace_rules(
            task.id, (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),), now=now
        )
        delivery = reminders.poll_due(now=now)[0].delivery
        assert delivery.id is not None
        if action == "acknowledge":
            reminders.acknowledge(delivery.id, now=now)
        elif action == "complete":
            reminders.complete(delivery.id, now=now)
        elif action == "defer":
            reminders.defer(delivery.id, now=now)
        elif action == "snooze":
            reminders.snooze(delivery.id, 10, now=now)
        elif action == "disable":
            reminders.replace_rules(
                task.id,
                (ReminderRuleInput(ReminderRelation.START, offset_minutes=0, enabled=False),),
                now=now,
            )
        elif action == "remove_rule":
            reminders.replace_rules(task.id, (), now=now)
        elif action.startswith("task_"):
            status = {
                "task_completed": TaskStatus.COMPLETED,
                "task_canceled": TaskStatus.CANCELED,
                "task_archived": TaskStatus.ARCHIVED,
            }[action]
            tasks.transition(task.id, status, now=now)
        else:
            tasks.move_to_trash(task.id, now=now)
            if action == "delete":
                tasks.delete_permanently(task.id)

    with _services(database) as (_tasks, restarted, _repository):
        assert restarted.poll_due(now=now + timedelta(minutes=1), recover_unhandled=True) == ()


def test_refired_old_snooze_is_recovered_using_last_fire_timestamp(tmp_path: Path) -> None:
    database = tmp_path / "officeflow.db"
    upgrade_database(database)
    now = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)
    with _services(database) as (tasks, reminders, _repository):
        task = tasks.create(TaskDraft(title="24시간 다시 알림 이후 복구", starts_at=now), now=now)
        assert task.id is not None
        reminders.replace_rules(
            task.id, (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),), now=now
        )
        original = reminders.poll_due(now=now)[0].delivery
        assert original.id is not None
        reminders.snooze(original.id, 1_440, now=now)
        refired = reminders.poll_due(now=now + timedelta(days=1))[0]

    with _services(database) as (_tasks, restarted, _repository):
        recovered = restarted.poll_due(
            now=now + timedelta(days=1, minutes=1), recover_unhandled=True
        )
        assert len(recovered) == 1
        assert recovered[0].delivery == refired.delivery


def test_delivery_claimed_before_poll_failure_is_recoverable_on_restart(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "officeflow.db"
    upgrade_database(database)
    now = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)
    with _services(database) as (tasks, reminders, repository):
        task = tasks.create(TaskDraft(title="표시 전 중단", starts_at=now), now=now)
        assert task.id is not None
        reminders.replace_rules(
            task.id, (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),), now=now
        )

        def fail_schedule_save(*_args, **_kwargs) -> None:
            raise OSError("알림 이력을 저장한 뒤 다음 시각 저장 실패")

        monkeypatch.setattr(repository, "save_reminder_schedule", fail_schedule_save)
        with pytest.raises(OSError):
            reminders.poll_due(now=now)
        persisted = repository.list_unhandled_reminder_targets(fired_since=now, due_at=now)
        assert len(persisted) == 1

    with _services(database) as (_tasks, restarted, _repository):
        recovered = restarted.poll_due(now=now + timedelta(minutes=1), recover_unhandled=True)
        assert len(recovered) == 1
        assert recovered[0].delivery == persisted[0][0]
        assert restarted.poll_due(now=now + timedelta(minutes=2)) == ()
