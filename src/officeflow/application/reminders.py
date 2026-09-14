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
from officeflow.domain.recurrence import expand_recurrence
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
        return self._repository.replace_reminders(task_id, rules)

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

        for target in self._repository.list_enabled_reminder_targets():
            for occurrence_start, scheduled_at in self._candidate_times(
                target,
                window_start=window_start,
                window_end=current,
            ):
                if occurrence_start is not None and self._occurrence_is_closed(
                    target.task.id, occurrence_start
                ):
                    continue
                reminder_id = target.reminder.id
                task_id = target.task.id
                if reminder_id is None or task_id is None:
                    continue
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
                if claimed is None:
                    continue
                alerts.append(
                    ReminderAlert(
                        delivery=claimed,
                        reminder=target.reminder,
                        task=target.task,
                        recovered=scheduled_at
                        < current - timedelta(seconds=recovery_threshold_seconds),
                    )
                )
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

    def _candidate_times(
        self,
        target: ReminderTarget,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[tuple[datetime | None, datetime], ...]:
        reminder = target.reminder
        task = target.task
        if reminder.relation is ReminderRelation.ABSOLUTE:
            if reminder.absolute_at is not None and window_start <= reminder.absolute_at <= window_end:
                return ((None, reminder.absolute_at),)
            return ()

        offset_minutes = reminder.offset_minutes or 0
        offset = timedelta(minutes=offset_minutes)
        base = task.starts_at if reminder.relation is ReminderRelation.START else task.ends_at
        if base is None:
            return ()
        if not task.recurrence_rule or task.starts_at is None:
            scheduled_at = self._relative_due(task, base, offset_minutes)
            return ((task.starts_at, scheduled_at),) if window_start <= scheduled_at <= window_end else ()

        duration = task.ends_at - task.starts_at if task.ends_at is not None else timedelta(0)
        relation_delta = duration if reminder.relation is ReminderRelation.END else timedelta(0)
        daylight_saving_margin = timedelta(hours=2)
        occurrence_window_start = (
            window_start - offset - relation_delta - daylight_saving_margin
        )
        occurrence_window_end = (
            window_end
            - offset
            - relation_delta
            + daylight_saving_margin
            + timedelta(microseconds=1)
        )
        candidates: list[tuple[datetime | None, datetime]] = []
        for occurrence_start, occurrence_end in expand_recurrence(
            task.recurrence_rule,
            template_start=task.starts_at,
            template_end=task.ends_at,
            timezone=task.timezone,
            range_start=occurrence_window_start,
            range_end=occurrence_window_end,
        ):
            occurrence_base = (
                occurrence_start
                if reminder.relation is ReminderRelation.START
                else occurrence_end
            )
            if occurrence_base is None:
                continue
            scheduled_at = self._relative_due(task, occurrence_base, offset_minutes)
            if window_start <= scheduled_at <= window_end:
                candidates.append((occurrence_start, scheduled_at))
        return tuple(candidates)

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
