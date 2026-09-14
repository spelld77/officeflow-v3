from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from officeflow.application.reminders import ReminderTarget
from officeflow.domain.enums import ReminderDeliveryStatus, ReminderRelation, TaskStatus
from officeflow.domain.occurrence import TaskOccurrence
from officeflow.domain.reminder import Reminder, ReminderDelivery, ReminderRuleInput
from officeflow.infrastructure.database.models import (
    ReminderDeliveryRecord,
    ReminderRecord,
    TaskOccurrenceRecord,
    TaskRecord,
)
from officeflow.infrastructure.database.session import SessionFactory
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository


class SqlAlchemyReminderRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def list_reminders(self, task_id: int) -> tuple[Reminder, ...]:
        statement = (
            select(ReminderRecord)
            .where(ReminderRecord.task_id == task_id)
            .order_by(ReminderRecord.relation, ReminderRecord.id)
        )
        with self._sessions.transaction() as session:
            return tuple(self._to_reminder(record) for record in session.scalars(statement).all())

    def replace_reminders(
        self,
        task_id: int,
        rules: tuple[ReminderRuleInput, ...],
    ) -> tuple[Reminder, ...]:
        with self._sessions.transaction() as session:
            if session.get(TaskRecord, task_id) is None:
                raise LookupError(f"업무 {task_id}을(를) 찾을 수 없습니다.")
            existing = list(
                session.scalars(
                    select(ReminderRecord).where(ReminderRecord.task_id == task_id)
                ).all()
            )
            requested = {rule.identity: rule for rule in rules}
            for record in existing:
                identity = (
                    ReminderRelation(record.relation),
                    record.offset_minutes,
                    record.absolute_at,
                )
                rule = requested.pop(identity, None)
                if rule is None:
                    session.delete(record)
                else:
                    record.enabled = rule.enabled
            for rule in requested.values():
                session.add(
                    ReminderRecord(
                        task_id=task_id,
                        relation=rule.relation.value,
                        offset_minutes=rule.offset_minutes,
                        absolute_at=rule.absolute_at,
                        enabled=rule.enabled,
                        last_fired_key=None,
                    )
                )
        return self.list_reminders(task_id)

    def list_enabled_reminder_targets(self) -> tuple[ReminderTarget, ...]:
        statement = (
            select(ReminderRecord, TaskRecord)
            .join(TaskRecord, TaskRecord.id == ReminderRecord.task_id)
            .where(
                ReminderRecord.enabled.is_(True),
                TaskRecord.deleted_at.is_(None),
                TaskRecord.status.in_((TaskStatus.ACTIVE.value, TaskStatus.PENDING.value)),
            )
            .order_by(ReminderRecord.id)
        )
        with self._sessions.transaction() as session:
            return tuple(
                ReminderTarget(
                    reminder=self._to_reminder(reminder),
                    task=SqlAlchemyTaskRepository._to_domain(task),
                )
                for reminder, task in session.execute(statement).all()
            )

    def get_reminder_target(self, reminder_id: int) -> ReminderTarget | None:
        statement = (
            select(ReminderRecord, TaskRecord)
            .join(TaskRecord, TaskRecord.id == ReminderRecord.task_id)
            .where(ReminderRecord.id == reminder_id, TaskRecord.deleted_at.is_(None))
        )
        with self._sessions.transaction() as session:
            row = session.execute(statement).one_or_none()
            if row is None:
                return None
            reminder, task = row
            return ReminderTarget(
                reminder=self._to_reminder(reminder),
                task=SqlAlchemyTaskRepository._to_domain(task),
            )

    def get_task_occurrence(
        self, task_id: int, occurrence_start: datetime
    ) -> TaskOccurrence | None:
        statement = select(TaskOccurrenceRecord).where(
            TaskOccurrenceRecord.task_id == task_id,
            TaskOccurrenceRecord.occurrence_start == occurrence_start,
        )
        with self._sessions.transaction() as session:
            record = session.scalar(statement)
            return (
                SqlAlchemyTaskRepository._occurrence_to_domain(record)
                if record is not None
                else None
            )

    def claim_reminder_delivery(
        self, delivery: ReminderDelivery
    ) -> ReminderDelivery | None:
        values = self._delivery_values(delivery)
        statement = (
            insert(ReminderDeliveryRecord)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["fire_key"])
            .returning(ReminderDeliveryRecord.id)
        )
        with self._sessions.transaction() as session:
            delivery_id = session.scalar(statement)
            if delivery_id is None:
                return None
            reminder = session.get(ReminderRecord, delivery.reminder_id)
            if reminder is not None:
                reminder.last_fired_key = delivery.fire_key
        return self.get_reminder_delivery(int(delivery_id))

    def get_reminder_delivery(self, delivery_id: int) -> ReminderDelivery | None:
        with self._sessions.transaction() as session:
            record = session.get(ReminderDeliveryRecord, delivery_id)
            return self._to_delivery(record) if record is not None else None

    def save_reminder_delivery(self, delivery: ReminderDelivery) -> ReminderDelivery:
        if delivery.id is None:
            raise ValueError("저장되지 않은 알림 이력은 수정할 수 없습니다.")
        with self._sessions.transaction() as session:
            record = session.get(ReminderDeliveryRecord, delivery.id)
            if record is None:
                raise LookupError(f"알림 {delivery.id}을(를) 찾을 수 없습니다.")
            for name, value in self._delivery_values(delivery).items():
                setattr(record, name, value)
        saved = self.get_reminder_delivery(delivery.id)
        if saved is None:
            raise LookupError(f"알림 {delivery.id}을(를) 찾을 수 없습니다.")
        return saved

    def list_snoozed_reminder_targets(
        self, due_at: datetime
    ) -> tuple[tuple[ReminderDelivery, ReminderTarget], ...]:
        statement = (
            select(ReminderDeliveryRecord, ReminderRecord, TaskRecord)
            .join(ReminderRecord, ReminderRecord.id == ReminderDeliveryRecord.reminder_id)
            .join(TaskRecord, TaskRecord.id == ReminderDeliveryRecord.task_id)
            .where(
                ReminderDeliveryRecord.status == ReminderDeliveryStatus.SNOOZED.value,
                ReminderDeliveryRecord.snoozed_until <= due_at,
                ReminderRecord.enabled.is_(True),
                TaskRecord.deleted_at.is_(None),
                TaskRecord.status.in_((TaskStatus.ACTIVE.value, TaskStatus.PENDING.value)),
            )
            .order_by(ReminderDeliveryRecord.snoozed_until, ReminderDeliveryRecord.id)
        )
        with self._sessions.transaction() as session:
            return tuple(
                (
                    self._to_delivery(delivery),
                    ReminderTarget(
                        reminder=self._to_reminder(reminder),
                        task=SqlAlchemyTaskRepository._to_domain(task),
                    ),
                )
                for delivery, reminder, task in session.execute(statement).all()
            )

    @staticmethod
    def _to_reminder(record: ReminderRecord) -> Reminder:
        return Reminder(
            id=record.id,
            task_id=record.task_id,
            relation=ReminderRelation(record.relation),
            offset_minutes=record.offset_minutes,
            absolute_at=record.absolute_at,
            enabled=record.enabled,
            last_fired_key=record.last_fired_key,
        )

    @staticmethod
    def _to_delivery(record: ReminderDeliveryRecord) -> ReminderDelivery:
        return ReminderDelivery(
            id=record.id,
            reminder_id=record.reminder_id,
            task_id=record.task_id,
            occurrence_start=record.occurrence_start,
            scheduled_at=record.scheduled_at,
            fire_key=record.fire_key,
            status=ReminderDeliveryStatus(record.status),
            first_fired_at=record.first_fired_at,
            last_fired_at=record.last_fired_at,
            snoozed_until=record.snoozed_until,
            acknowledged_at=record.acknowledged_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _delivery_values(delivery: ReminderDelivery) -> dict[str, object]:
        return {
            "reminder_id": delivery.reminder_id,
            "task_id": delivery.task_id,
            "occurrence_start": delivery.occurrence_start,
            "scheduled_at": delivery.scheduled_at,
            "fire_key": delivery.fire_key,
            "status": delivery.status.value,
            "first_fired_at": delivery.first_fired_at,
            "last_fired_at": delivery.last_fired_at,
            "snoozed_until": delivery.snoozed_until,
            "acknowledged_at": delivery.acknowledged_at,
            "created_at": delivery.created_at,
            "updated_at": delivery.updated_at,
        }
