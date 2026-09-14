from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from officeflow.application.reminders import (
    ReminderRepository,
    ReminderService,
    ReminderTarget,
)
from officeflow.application.tasks import TaskDraft, TaskService
from officeflow.domain.enums import (
    OccurrenceStatus,
    ReminderDeliveryStatus,
    ReminderRelation,
    TaskStatus,
)
from officeflow.domain.occurrence import TaskOccurrence
from officeflow.domain.reminder import (
    Reminder,
    ReminderDelivery,
    ReminderRuleInput,
    ReminderValidationError,
)
from tests.unit.test_task_service import InMemoryTaskRepository


class InMemoryReminderRepository(ReminderRepository):
    def __init__(self, tasks: InMemoryTaskRepository) -> None:
        self.tasks = tasks
        self.reminders: dict[int, Reminder] = {}
        self.deliveries: dict[int, ReminderDelivery] = {}
        self.next_reminder_id = 1
        self.next_delivery_id = 1

    def list_reminders(self, task_id: int) -> tuple[Reminder, ...]:
        return tuple(rule for rule in self.reminders.values() if rule.task_id == task_id)

    def replace_reminders(
        self, task_id: int, rules: tuple[ReminderRuleInput, ...]
    ) -> tuple[Reminder, ...]:
        existing = {reminder.identity: reminder for reminder in self.list_reminders(task_id)}
        keep: dict[int, Reminder] = {}
        for rule in rules:
            reminder = existing.get(rule.identity)
            if reminder is None:
                reminder = Reminder(
                    id=self.next_reminder_id,
                    task_id=task_id,
                    relation=rule.relation,
                    offset_minutes=rule.offset_minutes,
                    absolute_at=rule.absolute_at,
                    enabled=rule.enabled,
                )
                self.next_reminder_id += 1
            else:
                reminder = replace(reminder, enabled=rule.enabled)
            assert reminder.id is not None
            keep[reminder.id] = reminder
        removed = {
            reminder.id
            for reminder in self.list_reminders(task_id)
            if reminder.id not in keep
        }
        self.reminders = {
            reminder_id: reminder
            for reminder_id, reminder in self.reminders.items()
            if reminder_id not in removed
        }
        self.reminders.update(keep)
        return self.list_reminders(task_id)

    def list_enabled_reminder_targets(self) -> tuple[ReminderTarget, ...]:
        return tuple(
            ReminderTarget(reminder, self.tasks.tasks[reminder.task_id])
            for reminder in self.reminders.values()
            if reminder.enabled
            and self.tasks.tasks[reminder.task_id].status
            in {TaskStatus.ACTIVE, TaskStatus.PENDING}
        )

    def get_reminder_target(self, reminder_id: int) -> ReminderTarget | None:
        reminder = self.reminders.get(reminder_id)
        if reminder is None:
            return None
        return ReminderTarget(reminder, self.tasks.tasks[reminder.task_id])

    def get_task_occurrence(
        self, task_id: int, occurrence_start: datetime
    ) -> TaskOccurrence | None:
        return self.tasks.get_occurrence(task_id, occurrence_start)

    def claim_reminder_delivery(
        self, delivery: ReminderDelivery
    ) -> ReminderDelivery | None:
        if any(item.fire_key == delivery.fire_key for item in self.deliveries.values()):
            return None
        saved = replace(delivery, id=self.next_delivery_id)
        self.deliveries[self.next_delivery_id] = saved
        self.next_delivery_id += 1
        reminder = self.reminders[delivery.reminder_id]
        self.reminders[delivery.reminder_id] = replace(
            reminder, last_fired_key=delivery.fire_key
        )
        return saved

    def get_reminder_delivery(self, delivery_id: int) -> ReminderDelivery | None:
        return self.deliveries.get(delivery_id)

    def save_reminder_delivery(self, delivery: ReminderDelivery) -> ReminderDelivery:
        assert delivery.id is not None
        self.deliveries[delivery.id] = delivery
        return delivery

    def list_snoozed_reminder_targets(
        self, due_at: datetime
    ) -> tuple[tuple[ReminderDelivery, ReminderTarget], ...]:
        return tuple(
            (delivery, target)
            for delivery in self.deliveries.values()
            if delivery.status is ReminderDeliveryStatus.SNOOZED
            and delivery.snoozed_until is not None
            and delivery.snoozed_until <= due_at
            and (target := self.get_reminder_target(delivery.reminder_id)) is not None
        )


def build_services() -> tuple[TaskService, ReminderService, InMemoryReminderRepository]:
    task_repository = InMemoryTaskRepository()
    task_service = TaskService(task_repository, timezone="Asia/Seoul")
    reminder_repository = InMemoryReminderRepository(task_repository)
    return (
        task_service,
        ReminderService(reminder_repository, task_service),
        reminder_repository,
    )


def test_reminder_rule_requires_matching_value_type() -> None:
    with pytest.raises(ReminderValidationError):
        ReminderRuleInput(ReminderRelation.START)
    with pytest.raises(ReminderValidationError):
        ReminderRuleInput(ReminderRelation.ABSOLUTE, offset_minutes=-10)


def test_start_and_end_reminders_fire_once_at_their_due_times() -> None:
    task_service, reminder_service, _repository = build_services()
    task = task_service.create(
        TaskDraft(
            title="고객 미팅",
            starts_at=datetime(2026, 9, 14, 1, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 14, 2, 0, tzinfo=UTC),
        )
    )
    assert task.id is not None
    reminder_service.replace_rules(
        task.id,
        (
            ReminderRuleInput(ReminderRelation.START, offset_minutes=-10),
            ReminderRuleInput(ReminderRelation.END, offset_minutes=-15),
        ),
    )

    first = reminder_service.poll_due(now=datetime(2026, 9, 14, 1, 0, tzinfo=UTC))
    duplicate = reminder_service.poll_due(now=datetime(2026, 9, 14, 1, 1, tzinfo=UTC))
    second = reminder_service.poll_due(now=datetime(2026, 9, 14, 2, 0, tzinfo=UTC))

    assert [alert.reminder.relation for alert in first] == [ReminderRelation.START]
    assert duplicate == ()
    assert [alert.reminder.relation for alert in second] == [ReminderRelation.END]


def test_recent_missed_reminder_is_recovered_only_once() -> None:
    task_service, reminder_service, _repository = build_services()
    task = task_service.create(
        TaskDraft(title="놓친 업무", starts_at=datetime(2026, 9, 14, 1, 0, tzinfo=UTC))
    )
    assert task.id is not None
    reminder_service.replace_rules(
        task.id,
        (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),),
    )

    alerts = reminder_service.poll_due(now=datetime(2026, 9, 14, 2, 30, tzinfo=UTC))

    assert len(alerts) == 1
    assert alerts[0].recovered is True
    assert reminder_service.poll_due(now=datetime(2026, 9, 14, 2, 31, tzinfo=UTC)) == ()


def test_reminder_older_than_recovery_window_is_not_fired() -> None:
    task_service, reminder_service, _repository = build_services()
    task = task_service.create(
        TaskDraft(title="오래된 업무", starts_at=datetime(2026, 9, 14, 1, 0, tzinfo=UTC))
    )
    assert task.id is not None
    reminder_service.replace_rules(
        task.id,
        (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),),
    )

    assert (
        reminder_service.poll_due(now=datetime(2026, 9, 14, 3, 1, tzinfo=UTC)) == ()
    )


def test_all_day_morning_reminder_keeps_local_wall_clock_across_dst() -> None:
    task_repository = InMemoryTaskRepository()
    task_service = TaskService(task_repository, timezone="America/New_York")
    reminder_repository = InMemoryReminderRepository(task_repository)
    reminder_service = ReminderService(reminder_repository, task_service)
    task = task_service.create(
        TaskDraft(
            title="DST 당일 업무",
            all_day=True,
            starts_at=datetime(2026, 3, 8, 5, 0, tzinfo=UTC),
            ends_at=datetime(2026, 3, 9, 4, 0, tzinfo=UTC),
            timezone="America/New_York",
        )
    )
    assert task.id is not None
    reminder_service.replace_rules(
        task.id,
        (ReminderRuleInput(ReminderRelation.START, offset_minutes=540),),
    )

    alerts = reminder_service.poll_due(
        now=datetime(2026, 3, 8, 13, 0, tzinfo=UTC),
        grace_minutes=1,
    )

    assert len(alerts) == 1
    assert alerts[0].delivery.scheduled_at == datetime(2026, 3, 8, 13, 0, tzinfo=UTC)


def test_snoozed_reminder_refires_and_can_defer_task() -> None:
    task_service, reminder_service, _repository = build_services()
    task = task_service.create(
        TaskDraft(title="결재 확인", starts_at=datetime(2026, 9, 14, 1, 0, tzinfo=UTC))
    )
    assert task.id is not None
    reminder_service.replace_rules(
        task.id,
        (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),),
    )
    alert = reminder_service.poll_due(now=datetime(2026, 9, 14, 1, 0, tzinfo=UTC))[0]
    assert alert.delivery.id is not None

    snoozed = reminder_service.snooze(
        alert.delivery.id,
        10,
        now=datetime(2026, 9, 14, 1, 0, tzinfo=UTC),
    )
    assert snoozed.snoozed_until == datetime(2026, 9, 14, 1, 10, tzinfo=UTC)
    assert reminder_service.poll_due(now=datetime(2026, 9, 14, 1, 9, tzinfo=UTC)) == ()
    refired = reminder_service.poll_due(now=datetime(2026, 9, 14, 1, 10, tzinfo=UTC))
    assert len(refired) == 1
    assert refired[0].delivery.last_fired_at == datetime(2026, 9, 14, 1, 10, tzinfo=UTC)

    reminder_service.defer(alert.delivery.id, now=datetime(2026, 9, 14, 1, 11, tzinfo=UTC))

    assert task_service.get(task.id).status is TaskStatus.PENDING


def test_completed_recurrence_occurrence_suppresses_its_reminder_only() -> None:
    task_service, reminder_service, _repository = build_services()
    task = task_service.create(
        TaskDraft(
            title="매일 점검",
            starts_at=datetime(2026, 9, 14, 1, 0, tzinfo=UTC),
            recurrence_rule="FREQ=DAILY;INTERVAL=1",
        )
    )
    assert task.id is not None
    reminder_service.replace_rules(
        task.id,
        (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),),
    )
    task_service.transition_occurrence(
        task.id,
        datetime(2026, 9, 14, 1, 0, tzinfo=UTC),
        OccurrenceStatus.COMPLETED,
        now=datetime(2026, 9, 14, 1, 1, tzinfo=UTC),
    )

    first_day = reminder_service.poll_due(now=datetime(2026, 9, 14, 1, 1, tzinfo=UTC))
    second_day = reminder_service.poll_due(now=datetime(2026, 9, 15, 1, 0, tzinfo=UTC))

    assert first_day == ()
    assert len(second_day) == 1
    assert second_day[0].occurrence_start == datetime(2026, 9, 15, 1, 0, tzinfo=UTC)


def test_completing_recurring_alert_keeps_template_active() -> None:
    task_service, reminder_service, repository = build_services()
    task = task_service.create(
        TaskDraft(
            title="매일 보고",
            starts_at=datetime(2026, 9, 14, 1, 0, tzinfo=UTC),
            recurrence_rule="FREQ=DAILY;INTERVAL=1",
        )
    )
    assert task.id is not None
    reminder_service.replace_rules(
        task.id,
        (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),),
    )
    alert = reminder_service.poll_due(now=datetime(2026, 9, 14, 1, 0, tzinfo=UTC))[0]
    assert alert.delivery.id is not None

    reminder_service.complete(
        alert.delivery.id,
        now=datetime(2026, 9, 14, 1, 2, tzinfo=UTC),
    )

    assert task_service.get(task.id).status is TaskStatus.ACTIVE
    occurrence = repository.tasks.get_occurrence(
        task.id, datetime(2026, 9, 14, 1, 0, tzinfo=UTC)
    )
    assert occurrence is not None
    assert occurrence.status is OccurrenceStatus.COMPLETED


def test_snoozed_recurrence_alert_is_closed_when_occurrence_finishes_elsewhere() -> None:
    task_service, reminder_service, repository = build_services()
    occurrence_start = datetime(2026, 9, 14, 1, 0, tzinfo=UTC)
    task = task_service.create(
        TaskDraft(
            title="반복 점검",
            starts_at=occurrence_start,
            recurrence_rule="FREQ=DAILY;INTERVAL=1",
        )
    )
    assert task.id is not None
    reminder_service.replace_rules(
        task.id,
        (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),),
    )
    alert = reminder_service.poll_due(now=occurrence_start)[0]
    assert alert.delivery.id is not None
    reminder_service.snooze(alert.delivery.id, 10, now=occurrence_start)
    task_service.transition_occurrence(
        task.id,
        occurrence_start,
        OccurrenceStatus.COMPLETED,
        now=occurrence_start + timedelta(minutes=5),
    )

    assert reminder_service.poll_due(now=occurrence_start + timedelta(minutes=10)) == ()
    assert (
        repository.deliveries[alert.delivery.id].status
        is ReminderDeliveryStatus.ACKNOWLEDGED
    )
