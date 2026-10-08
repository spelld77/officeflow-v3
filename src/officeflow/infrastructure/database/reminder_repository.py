from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, tuple_, update
from sqlalchemy.dialects.sqlite import insert

from officeflow.application.reminder_runtime import ReminderRuntimeState
from officeflow.application.reminders import ReminderTarget
from officeflow.application.task_saving import ReminderEdit
from officeflow.domain.enums import (
    OccurrenceStatus,
    ReminderDeliveryStatus,
    ReminderRelation,
    TaskStatus,
)
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

    @contextmanager
    def read_snapshot(self) -> Iterator[SqlAlchemyReminderRepository]:
        with self._sessions.read_snapshot() as sessions:
            yield SqlAlchemyReminderRepository(sessions)

    def task_deliveries(self, task_id: int) -> tuple[ReminderDelivery, ...]:
        latest = select(ReminderDeliveryRecord.id,
            func.row_number().over(partition_by=ReminderDeliveryRecord.reminder_id,
                order_by=ReminderDeliveryRecord.updated_at.desc()).label("position")
            ).where(ReminderDeliveryRecord.task_id == task_id).subquery()
        statement = select(ReminderDeliveryRecord).where(ReminderDeliveryRecord.task_id == task_id,
            or_(ReminderDeliveryRecord.id.in_(select(latest.c.id).where(latest.c.position == 1)),
                ReminderDeliveryRecord.status.in_(("fired", "snoozed")))).order_by(ReminderDeliveryRecord.updated_at.desc()).limit(1000)
        with self._sessions.transaction() as session:
            return tuple(self._to_delivery(row) for row in session.scalars(statement))

    def occurrence_states(self, keys: tuple[tuple[int, datetime], ...]) -> dict[tuple[int, datetime], OccurrenceStatus]:
        result: dict[tuple[int, datetime], OccurrenceStatus] = {}
        with self._sessions.transaction() as session:
            for offset in range(0, len(keys), 200):
                statement = select(TaskOccurrenceRecord.task_id, TaskOccurrenceRecord.occurrence_start, TaskOccurrenceRecord.status).where(tuple_(TaskOccurrenceRecord.task_id, TaskOccurrenceRecord.occurrence_start).in_(keys[offset:offset + 200]))
                for task_id, start, status in session.execute(statement):
                    result[task_id, start] = OccurrenceStatus(status)
        return result

    def runtime_state(self, task_id: int) -> ReminderRuntimeState:
        with self.read_snapshot() as repo:
            tasks = SqlAlchemyTaskRepository(repo._sessions)
            task = tasks.get(task_id) or tasks.get_deleted(task_id)
            if task is None:
                raise ValueError("업무를 찾을 수 없습니다.")
            deliveries = repo.task_deliveries(task_id)
            reminders = repo.list_reminders(task_id)
            keys = {(task_id, d.occurrence_start) for d in deliveries if d.occurrence_start is not None}
            keys.update((task_id, r.next_occurrence_start) for r in reminders if r.next_occurrence_start is not None)
            from officeflow.application.reminders import ReminderService
            next_starts = []
            for reminder in reminders:
                next_item = ReminderService._next_candidate(ReminderTarget(reminder, task), after=datetime.now(UTC).replace(second=0, microsecond=0), inclusive=True)
                if next_item and next_item[0] is not None:
                    keys.add((task_id, next_item[0]))
                    next_starts.append(next_item[0])
            states = repo.occurrence_states(tuple(keys))
            if next_starts:
                # A completed current occurrence can be followed by another
                # explicitly skipped occurrence. Read these in one bounded
                # query, not per projected reminder row.
                statement = select(TaskOccurrenceRecord.occurrence_start, TaskOccurrenceRecord.status).where(
                    TaskOccurrenceRecord.task_id == task_id,
                    TaskOccurrenceRecord.occurrence_start >= min(next_starts),
                    TaskOccurrenceRecord.status.in_(("completed", "skipped", "canceled")),
                ).order_by(TaskOccurrenceRecord.occurrence_start).limit(512)
                with repo._sessions.transaction() as session:
                    states.update(((task_id, start), OccurrenceStatus(status)) for start, status in session.execute(statement))
            return ReminderRuntimeState(task, tuple((start, status) for (_id, start), status in states.items()), deliveries, reminders, datetime.now(UTC))

    @staticmethod
    def _target_matches(current: ReminderTarget | None, expected: ReminderTarget) -> bool:
        if current is None:
            return False
        r, old = current.reminder, expected.reminder
        t, old_task = current.task, expected.task
        return (r.identity, r.enabled, r.next_fire_at, r.next_occurrence_start, r.schedule_initialized) == (
            old.identity, old.enabled, old.next_fire_at, old.next_occurrence_start, old.schedule_initialized) and (
            t.starts_at, t.ends_at, t.timezone, t.all_day, t.recurrence_rule, t.status) == (
            old_task.starts_at, old_task.ends_at, old_task.timezone, old_task.all_day, old_task.recurrence_rule, old_task.status)

    def save_schedule_if_current(self, target: ReminderTarget, next_fire_at: datetime | None, next_occurrence_start: datetime | None) -> bool:
        assert target.reminder.id is not None
        with self._sessions.write_unit_of_work() as scoped:
            repo = SqlAlchemyReminderRepository(scoped)
            if not self._target_matches(repo.get_reminder_target(target.reminder.id), target):
                return False
            repo.save_reminder_schedule(target.reminder.id, next_fire_at, next_occurrence_start)
            return True

    def claim_current_delivery(self, delivery: ReminderDelivery, target: ReminderTarget) -> ReminderDelivery | None:
        with self._sessions.write_unit_of_work() as scoped:
            repo = SqlAlchemyReminderRepository(scoped)
            if not self._target_matches(repo.get_reminder_target(delivery.reminder_id), target):
                return None
            return repo.claim_reminder_delivery(delivery)

    def preview_targets(self, start: datetime, end: datetime, *, after_id: int, limit: int) -> tuple[ReminderTarget, ...]:
        margin = timedelta(days=32)
        statement = select(ReminderRecord, TaskRecord, SqlAlchemyTaskRepository._attachment_exists()).join(TaskRecord, TaskRecord.id == ReminderRecord.task_id).where(
            ReminderRecord.id > after_id, ReminderRecord.enabled.is_(True), TaskRecord.deleted_at.is_(None),
            TaskRecord.status.in_(("active", "pending")),
            or_(
                (ReminderRecord.relation == "absolute") & (ReminderRecord.absolute_at >= start) & (ReminderRecord.absolute_at < end),
                (ReminderRecord.relation == "start") & (TaskRecord.starts_at >= start - margin) & (TaskRecord.starts_at < end + margin),
                (ReminderRecord.relation == "end") & (TaskRecord.ends_at >= start - margin) & (TaskRecord.ends_at < end + margin),
                (ReminderRecord.relation.in_(("start", "end"))) & TaskRecord.recurrence_rule.is_not(None),
            )
        ).order_by(ReminderRecord.id).limit(limit)
        with self._sessions.transaction() as session:
            return tuple(ReminderTarget(self._to_reminder(r), SqlAlchemyTaskRepository._to_domain(t, has_attachments=bool(attached))) for r, t, attached in session.execute(statement))

    def preview_deliveries(self, start: datetime, end: datetime, *, limit: int) -> tuple[tuple[ReminderDelivery, ReminderTarget], ...]:
        statement = select(ReminderDeliveryRecord, ReminderRecord, TaskRecord, SqlAlchemyTaskRepository._attachment_exists()).join(
            ReminderRecord, ReminderRecord.id == ReminderDeliveryRecord.reminder_id).join(
            TaskRecord, TaskRecord.id == ReminderDeliveryRecord.task_id).where(TaskRecord.deleted_at.is_(None),
            or_((ReminderDeliveryRecord.scheduled_at >= start) & (ReminderDeliveryRecord.scheduled_at < end),
                (ReminderDeliveryRecord.snoozed_until >= start) & (ReminderDeliveryRecord.snoozed_until < end),
                (ReminderDeliveryRecord.last_fired_at >= start) & (ReminderDeliveryRecord.last_fired_at < end))
        ).order_by(ReminderDeliveryRecord.scheduled_at, ReminderDeliveryRecord.id).limit(limit)
        with self._sessions.transaction() as session:
            return tuple((self._to_delivery(d), ReminderTarget(self._to_reminder(r), SqlAlchemyTaskRepository._to_domain(t, has_attachments=bool(attached)))) for d, r, t, attached in session.execute(statement))

    def closed_occurrences(self, task_ids: tuple[int, ...], start: datetime, end: datetime) -> frozenset[tuple[int, datetime]]:
        if not task_ids:
            return frozenset()
        statement = select(TaskOccurrenceRecord.task_id, TaskOccurrenceRecord.occurrence_start).where(
            TaskOccurrenceRecord.task_id.in_(task_ids), TaskOccurrenceRecord.occurrence_start >= start,
            TaskOccurrenceRecord.occurrence_start < end, TaskOccurrenceRecord.status.in_(("completed", "skipped", "canceled")))
        with self._sessions.transaction() as session:
            return frozenset((task_id, occurrence) for task_id, occurrence in session.execute(statement))

    def list_reminders(self, task_id: int) -> tuple[Reminder, ...]:
        statement = (
            select(ReminderRecord)
            .where(ReminderRecord.task_id == task_id)
            .order_by(ReminderRecord.relation, ReminderRecord.id)
        )
        with self._sessions.transaction() as session:
            return tuple(self._to_reminder(record) for record in session.scalars(statement).all())

    def dismiss_rule_deliveries(self, reminder_id: int, now: datetime) -> None:
        with self._sessions.transaction() as session:
            session.execute(update(ReminderDeliveryRecord).where(
                ReminderDeliveryRecord.reminder_id == reminder_id,
                ReminderDeliveryRecord.status.in_(("fired", "snoozed")),
            ).values(status="acknowledged", acknowledged_at=now, updated_at=now, snoozed_until=None))

    def apply_edits(self, task_id: int, edits: tuple[ReminderEdit, ...], *, now: datetime) -> tuple[Reminder, ...]:
        with self._sessions.write_unit_of_work() as scoped:
            with scoped.transaction() as session:
                task = session.get(TaskRecord, task_id)
                if task is None or task.deleted_at is not None:
                    raise LookupError("업무를 찾을 수 없거나 휴지통에 있습니다.")
                existing = {r.id: r for r in session.scalars(select(ReminderRecord).where(ReminderRecord.task_id == task_id))}
                if len(edits) > 20 and (len(edits) > len(existing) or any(e.reminder_id is None for e in edits)):
                    raise ValueError("업무 하나에 알림을 최대 20개까지 설정할 수 있습니다.")
                ids = [e.reminder_id for e in edits if e.reminder_id is not None]
                if len(ids) != len(set(ids)) or not set(ids) <= existing.keys():
                    raise ValueError("올바른 알림을 선택하세요.")
                if len({e.rule.identity for e in edits}) != len(edits):
                    raise ValueError("중복 알림은 저장할 수 없습니다.")
                for reminder_id, existing_record in existing.items():
                    if reminder_id not in ids:
                        session.delete(existing_record)
                for edit in edits:
                    rule = edit.rule
                    record = existing.get(edit.reminder_id) if edit.reminder_id is not None else None
                    if record is None:
                        record = ReminderRecord(task_id=task_id, relation=rule.relation.value,
                            offset_minutes=rule.offset_minutes, absolute_at=rule.absolute_at,
                            enabled=rule.enabled, schedule_initialized=False)
                        session.add(record)
                    elif self._to_reminder(record).identity != rule.identity or record.enabled != rule.enabled:
                        SqlAlchemyReminderRepository(scoped).dismiss_rule_deliveries(record.id, now)
                        record.relation = rule.relation.value
                        record.offset_minutes = rule.offset_minutes
                        record.absolute_at = rule.absolute_at
                        record.enabled = rule.enabled
                        record.next_fire_at = None
                        record.next_occurrence_start = None
                        record.schedule_initialized = False
                session.flush()
            return SqlAlchemyReminderRepository(scoped).list_reminders(task_id)

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
            if len(rules) > 20 and (len(rules) > len(existing) or set(requested) - {self._to_reminder(r).identity for r in existing}):
                raise ValueError("업무 하나에 알림을 최대 20개까지 설정할 수 있습니다.")
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
                    if record.enabled != rule.enabled:
                        record.enabled = rule.enabled
                        record.next_fire_at = None
                        record.next_occurrence_start = None
                        record.schedule_initialized = False
            for rule in requested.values():
                session.add(
                    ReminderRecord(
                        task_id=task_id,
                        relation=rule.relation.value,
                        offset_minutes=rule.offset_minutes,
                        absolute_at=rule.absolute_at,
                        enabled=rule.enabled,
                        last_fired_key=None,
                        next_fire_at=None,
                        next_occurrence_start=None,
                        schedule_initialized=False,
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

    def list_due_reminder_targets(
        self, due_at: datetime, *, limit: int = 200
    ) -> tuple[ReminderTarget, ...]:
        statement = (
            select(ReminderRecord, TaskRecord)
            .join(TaskRecord, TaskRecord.id == ReminderRecord.task_id)
            .where(
                ReminderRecord.enabled.is_(True),
                ReminderRecord.next_fire_at.is_not(None),
                ReminderRecord.next_fire_at <= due_at,
                TaskRecord.deleted_at.is_(None),
                TaskRecord.status.in_((TaskStatus.ACTIVE.value, TaskStatus.PENDING.value)),
            )
            .order_by(ReminderRecord.next_fire_at, ReminderRecord.id)
            .limit(limit)
        )
        with self._sessions.transaction() as session:
            return tuple(
                ReminderTarget(
                    reminder=self._to_reminder(reminder),
                    task=SqlAlchemyTaskRepository._to_domain(task),
                )
                for reminder, task in session.execute(statement).all()
            )

    def list_unscheduled_reminder_targets(
        self, *, limit: int = 100
    ) -> tuple[ReminderTarget, ...]:
        statement = (
            select(ReminderRecord, TaskRecord)
            .join(TaskRecord, TaskRecord.id == ReminderRecord.task_id)
            .where(
                ReminderRecord.enabled.is_(True),
                ReminderRecord.schedule_initialized.is_(False),
                TaskRecord.deleted_at.is_(None),
                TaskRecord.status.in_((TaskStatus.ACTIVE.value, TaskStatus.PENDING.value)),
            )
            .order_by(ReminderRecord.id)
            .limit(limit)
        )
        with self._sessions.transaction() as session:
            return tuple(
                ReminderTarget(
                    reminder=self._to_reminder(reminder),
                    task=SqlAlchemyTaskRepository._to_domain(task),
                )
                for reminder, task in session.execute(statement).all()
            )

    def save_reminder_schedule(
        self,
        reminder_id: int,
        next_fire_at: datetime | None,
        next_occurrence_start: datetime | None,
    ) -> None:
        with self._sessions.transaction() as session:
            record = session.get(ReminderRecord, reminder_id)
            if record is None:
                raise LookupError(f"알림 규칙 {reminder_id}을(를) 찾을 수 없습니다.")
            record.next_fire_at = next_fire_at
            record.next_occurrence_start = next_occurrence_start
            record.schedule_initialized = True

    def restore_reminder_schedule(self, reminder: Reminder) -> None:
        with self._sessions.transaction() as session:
            session.execute(update(ReminderRecord).where(ReminderRecord.id == reminder.id).values(
                next_fire_at=reminder.next_fire_at, next_occurrence_start=reminder.next_occurrence_start,
                schedule_initialized=reminder.schedule_initialized))

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
            reminder = session.get(ReminderRecord, delivery.reminder_id)
            task = session.get(TaskRecord, delivery.task_id)
            if reminder is None or not reminder.enabled or task is None or task.deleted_at or task.status not in {"active", "pending"}:
                return None
            if delivery.occurrence_start is not None:
                closed = session.scalar(select(TaskOccurrenceRecord.id).where(
                    TaskOccurrenceRecord.task_id == delivery.task_id,
                    TaskOccurrenceRecord.occurrence_start == delivery.occurrence_start,
                    TaskOccurrenceRecord.status.in_(("completed", "skipped", "canceled"))))
                if closed is not None:
                    return None
            delivery_id = session.scalar(statement)
            if delivery_id is None:
                return None
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
            if record.status in {"acknowledged", "completed", "deferred"}:
                return self._to_delivery(record)
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

    def list_unhandled_reminder_targets(
        self, *, fired_since: datetime, due_at: datetime
    ) -> tuple[tuple[ReminderDelivery, ReminderTarget], ...]:
        statement = (
            select(ReminderDeliveryRecord, ReminderRecord, TaskRecord)
            .join(ReminderRecord, ReminderRecord.id == ReminderDeliveryRecord.reminder_id)
            .join(TaskRecord, TaskRecord.id == ReminderDeliveryRecord.task_id)
            .where(
                ReminderDeliveryRecord.status == ReminderDeliveryStatus.FIRED.value,
                ReminderDeliveryRecord.acknowledged_at.is_(None),
                ReminderDeliveryRecord.last_fired_at >= fired_since,
                ReminderDeliveryRecord.last_fired_at <= due_at,
                ReminderRecord.enabled.is_(True),
                TaskRecord.deleted_at.is_(None),
                TaskRecord.status.in_((TaskStatus.ACTIVE.value, TaskStatus.PENDING.value)),
            )
            .order_by(ReminderDeliveryRecord.scheduled_at, ReminderDeliveryRecord.id)
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

    def list_active_snoozed_deliveries(self) -> tuple[ReminderDelivery, ...]:
        statement = (
            select(ReminderDeliveryRecord)
            .join(ReminderRecord, ReminderRecord.id == ReminderDeliveryRecord.reminder_id)
            .join(TaskRecord, TaskRecord.id == ReminderDeliveryRecord.task_id)
            .where(
                ReminderDeliveryRecord.status == ReminderDeliveryStatus.SNOOZED.value,
                ReminderDeliveryRecord.snoozed_until.is_not(None),
                ReminderRecord.enabled.is_(True),
                TaskRecord.deleted_at.is_(None),
                TaskRecord.status.in_((TaskStatus.ACTIVE.value, TaskStatus.PENDING.value)),
            )
            .order_by(ReminderDeliveryRecord.snoozed_until, ReminderDeliveryRecord.id)
        )
        with self._sessions.transaction() as session:
            return tuple(
                self._to_delivery(record) for record in session.scalars(statement).all()
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
            next_fire_at=record.next_fire_at,
            next_occurrence_start=record.next_occurrence_start,
            schedule_initialized=record.schedule_initialized,
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
