from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from officeflow.application.tasks import TaskService
from officeflow.domain.enums import (
    OccurrenceStatus,
    ReminderDeliveryStatus,
    ReminderRelation,
    TaskStatus,
)
from officeflow.domain.occurrence import TaskOccurrence
from officeflow.domain.recurrence import next_recurrence_start
from officeflow.domain.reminder import Reminder, ReminderDelivery, ReminderRuleInput
from officeflow.domain.task import Task


@dataclass(frozen=True, slots=True)
class ReminderTarget:
    reminder: Reminder
    task: Task


@dataclass(frozen=True, slots=True)
class ReminderAlert:
    delivery: ReminderDelivery
    reminder: Reminder
    task: Task
    recovered: bool = False

    @property
    def occurrence_start(self) -> datetime | None:
        return self.delivery.occurrence_start


class ReminderRepository(Protocol):
    def list_reminders(self, task_id: int) -> tuple[Reminder, ...]: ...

    def replace_reminders(
        self, task_id: int, rules: tuple[ReminderRuleInput, ...]
    ) -> tuple[Reminder, ...]: ...

    def list_enabled_reminder_targets(self) -> tuple[ReminderTarget, ...]: ...

    def list_due_reminder_targets(
        self, due_at: datetime, *, limit: int = 200
    ) -> tuple[ReminderTarget, ...]: ...

    def list_unscheduled_reminder_targets(
        self, *, limit: int = 100
    ) -> tuple[ReminderTarget, ...]: ...

    def save_reminder_schedule(
        self,
        reminder_id: int,
        next_fire_at: datetime | None,
        next_occurrence_start: datetime | None,
    ) -> None: ...

    def get_reminder_target(self, reminder_id: int) -> ReminderTarget | None: ...

    def get_task_occurrence(
        self, task_id: int, occurrence_start: datetime
    ) -> TaskOccurrence | None: ...

    def claim_reminder_delivery(
        self, delivery: ReminderDelivery
    ) -> ReminderDelivery | None: ...

    def get_reminder_delivery(self, delivery_id: int) -> ReminderDelivery | None: ...

    def save_reminder_delivery(self, delivery: ReminderDelivery) -> ReminderDelivery: ...

    def list_snoozed_reminder_targets(
        self, due_at: datetime
    ) -> tuple[tuple[ReminderDelivery, ReminderTarget], ...]: ...


class ReminderService:
    def __init__(self, repository: ReminderRepository, task_service: TaskService) -> None:
        self._repository = repository
        self._task_service = task_service

    def rules_for_task(self, task_id: int) -> tuple[ReminderRuleInput, ...]:
        return tuple(
            ReminderRuleInput(
                relation=reminder.relation,
                offset_minutes=reminder.offset_minutes,
                absolute_at=reminder.absolute_at,
                enabled=reminder.enabled,
            )
            for reminder in self._repository.list_reminders(task_id)
        )

    def replace_rules(
        self,
        task_id: int,
        rules: tuple[ReminderRuleInput, ...],
        *,
        now: datetime | None = None,
    ) -> tuple[Reminder, ...]:
        task = self._task_service.get(task_id)
        for rule in rules:
            if rule.relation is ReminderRelation.START and task.starts_at is None:
                raise ValueError("시작 알림에는 시작 일정이 필요합니다.")
            if rule.relation is ReminderRelation.END and task.ends_at is None:
                raise ValueError("종료 알림에는 종료 일정이 필요합니다.")
        identities = [rule.identity for rule in rules]
        if len(identities) != len(set(identities)):
            raise ValueError("동일한 알림 규칙을 중복으로 저장할 수 없습니다.")
        reminders = self._repository.replace_reminders(task_id, rules)
        for reminder in reminders:
            if reminder.id is None:
                continue
            candidate = None
            if reminder.enabled:
                target = ReminderTarget(reminder=reminder, task=task)
                schedule_from = (
                    now - timedelta(minutes=120)
                    if now is not None
                    else self._initial_schedule_anchor(target)
                )
                candidate = self._next_candidate(
                    target,
                    after=schedule_from,
                    inclusive=True,
                )
            self._repository.save_reminder_schedule(
                reminder.id,
                candidate[1] if candidate is not None else None,
                candidate[0] if candidate is not None else None,
            )
        return self._repository.list_reminders(task_id)

    def poll_due(
        self,
        *,
        now: datetime | None = None,
        grace_minutes: int = 120,
        recovery_threshold_seconds: int = 90,
    ) -> tuple[ReminderAlert, ...]:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("알림 조회 시각에 시간대 정보가 필요합니다.")
        if not 1 <= grace_minutes <= 43_200:
            raise ValueError("놓친 알림 복구 범위는 1분에서 30일 사이여야 합니다.")
        window_start = current - timedelta(minutes=grace_minutes)
        alerts: list[ReminderAlert] = []

        for delivery, target in self._repository.list_snoozed_reminder_targets(current):
            if delivery.occurrence_start is not None and self._occurrence_is_closed(
                delivery.task_id, delivery.occurrence_start
            ):
                self._repository.save_reminder_delivery(delivery.acknowledge(now=current))
                continue
            refired = self._repository.save_reminder_delivery(delivery.refire(now=current))
            alerts.append(
                ReminderAlert(
                    delivery=refired,
                    reminder=target.reminder,
                    task=target.task,
                    recovered=False,
                )
            )

        while True:
            unscheduled = self._repository.list_unscheduled_reminder_targets(limit=100)
            for target in unscheduled:
                reminder_id = target.reminder.id
                if reminder_id is None:
                    continue
                candidate = self._next_candidate(
                    target,
                    after=window_start,
                    inclusive=True,
                )
                self._repository.save_reminder_schedule(
                    reminder_id,
                    candidate[1] if candidate is not None else None,
                    candidate[0] if candidate is not None else None,
                )
            if len(unscheduled) < 100:
                break

        while True:
            due_targets = self._repository.list_due_reminder_targets(current, limit=200)
            for target in due_targets:
                reminder_id = target.reminder.id
                task_id = target.task.id
                if reminder_id is None or task_id is None:
                    continue
                occurrence_start = target.reminder.next_occurrence_start
                scheduled_at = target.reminder.next_fire_at
                while scheduled_at is not None and scheduled_at <= current:
                    if scheduled_at >= window_start and not (
                        occurrence_start is not None
                        and self._occurrence_is_closed(task_id, occurrence_start)
                    ):
                        delivery = ReminderDelivery.fired(
                            reminder_id=reminder_id,
                            task_id=task_id,
                            occurrence_start=occurrence_start,
                            scheduled_at=scheduled_at,
                            fire_key=self.fire_key(
                                reminder_id=reminder_id,
                                task_id=task_id,
                                occurrence_start=occurrence_start,
                                scheduled_at=scheduled_at,
                            ),
                            now=current,
                        )
                        claimed = self._repository.claim_reminder_delivery(delivery)
                        if claimed is not None:
                            alerts.append(
                                ReminderAlert(
                                    delivery=claimed,
                                    reminder=target.reminder,
                                    task=target.task,
                                    recovered=scheduled_at
                                    < current
                                    - timedelta(seconds=recovery_threshold_seconds),
                                )
                            )
                    candidate = self._next_candidate(
                        target,
                        after=scheduled_at,
                        inclusive=False,
                    )
                    if candidate is None:
                        occurrence_start = None
                        scheduled_at = None
                    else:
                        occurrence_start, scheduled_at = candidate
                self._repository.save_reminder_schedule(
                    reminder_id,
                    scheduled_at,
                    occurrence_start,
                )
            if len(due_targets) < 200:
                break
        return tuple(sorted(alerts, key=lambda alert: alert.delivery.scheduled_at))

    def snooze(
        self, delivery_id: int, minutes: int = 10, *, now: datetime | None = None
    ) -> ReminderDelivery:
        delivery = self._get_delivery(delivery_id)
        return self._repository.save_reminder_delivery(
            delivery.snooze(minutes, now=now or datetime.now(UTC))
        )

    def acknowledge(
        self, delivery_id: int, *, now: datetime | None = None
    ) -> ReminderDelivery:
        delivery = self._get_delivery(delivery_id)
        return self._repository.save_reminder_delivery(delivery.acknowledge(now=now))

    def complete(
        self, delivery_id: int, *, now: datetime | None = None
    ) -> ReminderDelivery:
        current = now or datetime.now(UTC)
        delivery = self._get_delivery(delivery_id)
        task = self._task_service.get(delivery.task_id)
        if task.recurrence_rule and delivery.occurrence_start is not None:
            self._task_service.transition_occurrence(
                task.id or delivery.task_id,
                delivery.occurrence_start,
                OccurrenceStatus.COMPLETED,
                now=current,
            )
        elif task.status is not TaskStatus.COMPLETED:
            self._task_service.transition(delivery.task_id, TaskStatus.COMPLETED, now=current)
        return self._repository.save_reminder_delivery(
            delivery.acknowledge(ReminderDeliveryStatus.COMPLETED, now=current)
        )

    def defer(self, delivery_id: int, *, now: datetime | None = None) -> ReminderDelivery:
        current = now or datetime.now(UTC)
        delivery = self._get_delivery(delivery_id)
        task = self._task_service.get(delivery.task_id)
        if task.status is TaskStatus.ACTIVE:
            self._task_service.transition(delivery.task_id, TaskStatus.PENDING, now=current)
        return self._repository.save_reminder_delivery(
            delivery.acknowledge(ReminderDeliveryStatus.DEFERRED, now=current)
        )

    @staticmethod
    def fire_key(
        *,
        reminder_id: int,
        task_id: int,
        occurrence_start: datetime | None,
        scheduled_at: datetime,
    ) -> str:
        occurrence = occurrence_start.astimezone(UTC).isoformat() if occurrence_start else "single"
        scheduled = scheduled_at.astimezone(UTC).isoformat()
        return f"task:{task_id}|occurrence:{occurrence}|reminder:{reminder_id}|at:{scheduled}"

    def _next_candidate(
        self,
        target: ReminderTarget,
        *,
        after: datetime,
        inclusive: bool,
    ) -> tuple[datetime | None, datetime] | None:
        reminder = target.reminder
        task = target.task
        if reminder.relation is ReminderRelation.ABSOLUTE:
            if reminder.absolute_at is None:
                return None
            if reminder.absolute_at > after or (
                inclusive and reminder.absolute_at == after
            ):
                return None, reminder.absolute_at
            return None

        offset_minutes = reminder.offset_minutes or 0
        offset = timedelta(minutes=offset_minutes)
        base = task.starts_at if reminder.relation is ReminderRelation.START else task.ends_at
        if base is None:
            return None
        if not task.recurrence_rule or task.starts_at is None:
            scheduled_at = self._relative_due(task, base, offset_minutes)
            if scheduled_at > after or (inclusive and scheduled_at == after):
                return task.starts_at, scheduled_at
            return None

        duration = task.ends_at - task.starts_at if task.ends_at is not None else timedelta(0)
        relation_delta = duration if reminder.relation is ReminderRelation.END else timedelta(0)
        daylight_saving_margin = timedelta(hours=2)
        occurrence_cursor = after - offset - relation_delta - daylight_saving_margin
        occurrence_start = next_recurrence_start(
            task.recurrence_rule,
            template_start=task.starts_at,
            timezone=task.timezone,
            after=occurrence_cursor,
            inclusive=True,
        )
        while occurrence_start is not None:
            occurrence_end = occurrence_start + duration if task.ends_at is not None else None
            occurrence_base = (
                occurrence_start
                if reminder.relation is ReminderRelation.START
                else occurrence_end
            )
            if occurrence_base is None:
                return None
            scheduled_at = self._relative_due(task, occurrence_base, offset_minutes)
            if scheduled_at > after or (inclusive and scheduled_at == after):
                return occurrence_start, scheduled_at
            occurrence_start = next_recurrence_start(
                task.recurrence_rule,
                template_start=task.starts_at,
                timezone=task.timezone,
                after=occurrence_start,
                inclusive=False,
            )
        return None

    @staticmethod
    def _initial_schedule_anchor(target: ReminderTarget) -> datetime:
        base = target.reminder.absolute_at or target.task.starts_at or target.task.ends_at
        if base is None:
            return datetime.now(UTC)
        return base - timedelta(days=31)

    @staticmethod
    def _relative_due(task: Task, base: datetime, offset_minutes: int) -> datetime:
        zone = ZoneInfo(task.timezone)
        return (base.astimezone(zone) + timedelta(minutes=offset_minutes)).astimezone(UTC)

    def _occurrence_is_closed(self, task_id: int | None, occurrence_start: datetime) -> bool:
        if task_id is None:
            return True
        occurrence = self._repository.get_task_occurrence(task_id, occurrence_start)
        return occurrence is not None and occurrence.status in {
            OccurrenceStatus.COMPLETED,
            OccurrenceStatus.SKIPPED,
            OccurrenceStatus.CANCELED,
        }

    def _get_delivery(self, delivery_id: int) -> ReminderDelivery:
        delivery = self._repository.get_reminder_delivery(delivery_id)
        if delivery is None:
            raise LookupError(f"알림 {delivery_id}을(를) 찾을 수 없습니다.")
        return delivery
