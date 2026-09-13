from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, case, func, or_, select

from officeflow.application.tasks import TaskGroup, TaskPage, TaskQuery, TaskSort, TaskView
from officeflow.domain.enums import TaskPriority, TaskStatus
from officeflow.domain.task import Task
from officeflow.infrastructure.database.models import TaskRecord
from officeflow.infrastructure.database.session import SessionFactory


class SqlAlchemyTaskRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def add(self, task: Task) -> Task:
        record = self._to_record(task)
        with self._sessions.transaction() as session:
            session.add(record)
            session.flush()
            task_id = record.id
        return self.get_required(task_id)

    def update(self, task: Task) -> Task:
        if task.id is None:
            raise ValueError("저장되지 않은 업무는 수정할 수 없습니다.")
        with self._sessions.transaction() as session:
            record = session.get(TaskRecord, task.id)
            if record is None or record.deleted_at is not None:
                raise LookupError(f"업무 {task.id}을(를) 찾을 수 없습니다.")
            self._copy_to_record(task, record)
        return self.get_required(task.id)

    def get(self, task_id: int) -> Task | None:
        with self._sessions.transaction() as session:
            record = session.get(TaskRecord, task_id)
            if record is None or record.deleted_at is not None:
                return None
            return self._to_domain(record)

    def get_required(self, task_id: int) -> Task:
        task = self.get(task_id)
        if task is None:
            raise LookupError(f"업무 {task_id}을(를) 찾을 수 없습니다.")
        return task

    def query(
        self,
        query: TaskQuery,
        *,
        current: datetime,
        day_start: datetime,
        day_end: datetime,
    ) -> TaskPage:
        predicates = self._predicates(
            query,
            current=current,
            day_start=day_start,
            day_end=day_end,
        )
        count_statement = select(func.count(TaskRecord.id)).where(*predicates)
        statement: Select[tuple[TaskRecord]] = (
            select(TaskRecord).where(*predicates).order_by(*self._order_by(query.sort))
        )
        if query.offset:
            statement = statement.offset(query.offset)
        if query.limit is not None:
            statement = statement.limit(query.limit)
        with self._sessions.transaction() as session:
            total = int(session.scalar(count_statement) or 0)
            items = tuple(self._to_domain(record) for record in session.scalars(statement).all())
        return TaskPage(items=items, total=total, offset=query.offset, limit=query.limit)

    @classmethod
    def _predicates(
        cls,
        query: TaskQuery,
        *,
        current: datetime,
        day_start: datetime,
        day_end: datetime,
    ) -> list[Any]:
        predicates: list[Any] = [TaskRecord.deleted_at.is_(None)]
        if query.group is None:
            predicates.extend(cls._view_predicates(query.view, day_start, day_end))
        else:
            predicates.extend(cls._group_predicates(query.group, current, day_start, day_end))

        normalized = query.search.strip()
        if normalized:
            escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            predicates.append(
                TaskRecord.title.ilike(pattern, escape="\\")
                | TaskRecord.description.ilike(pattern, escape="\\")
            )
        if query.statuses:
            predicates.append(
                TaskRecord.status.in_(tuple(status.value for status in query.statuses))
            )
        if query.priorities:
            predicates.append(
                TaskRecord.priority.in_(tuple(priority.value for priority in query.priorities))
            )
        if query.pinned_only:
            predicates.append(TaskRecord.is_pinned.is_(True))
        return predicates

    @staticmethod
    def _view_predicates(view: TaskView, day_start: datetime, day_end: datetime) -> list[Any]:
        active = (TaskStatus.ACTIVE.value, TaskStatus.PENDING.value)
        if view is TaskView.TODAY:
            return [
                TaskRecord.status != TaskStatus.ARCHIVED.value,
                TaskRecord.starts_at.is_not(None),
                TaskRecord.starts_at < day_end,
                or_(
                    TaskRecord.ends_at > day_start,
                    and_(TaskRecord.ends_at.is_(None), TaskRecord.starts_at >= day_start),
                ),
            ]
        if view is TaskView.UPCOMING:
            return [TaskRecord.status.in_(active), TaskRecord.starts_at >= day_end]
        if view is TaskView.IMPORTANT:
            return [
                TaskRecord.status.in_(active),
                TaskRecord.priority.in_((TaskPriority.IMPORTANT.value, TaskPriority.URGENT.value)),
            ]
        if view is TaskView.PENDING:
            return [TaskRecord.status == TaskStatus.PENDING.value]
        if view is TaskView.COMPLETED:
            return [TaskRecord.status == TaskStatus.COMPLETED.value]
        return [TaskRecord.status != TaskStatus.ARCHIVED.value]

    @staticmethod
    def _group_predicates(
        group: TaskGroup,
        current: datetime,
        day_start: datetime,
        day_end: datetime,
    ) -> list[Any]:
        active = (TaskStatus.ACTIVE.value, TaskStatus.PENDING.value)
        if group is TaskGroup.OVERDUE:
            return [
                TaskRecord.status.in_(active),
                TaskRecord.starts_at.is_not(None),
                or_(
                    TaskRecord.ends_at <= current,
                    and_(TaskRecord.ends_at.is_(None), TaskRecord.starts_at < current),
                ),
            ]
        if group is TaskGroup.IN_PROGRESS:
            return [
                TaskRecord.status.in_(active),
                TaskRecord.starts_at <= current,
                TaskRecord.ends_at > current,
            ]
        if group is TaskGroup.UPCOMING:
            return [
                TaskRecord.status.in_(active),
                or_(
                    TaskRecord.starts_at > current,
                    and_(TaskRecord.starts_at == current, TaskRecord.ends_at.is_(None)),
                ),
                TaskRecord.starts_at < day_end,
            ]
        return [
            TaskRecord.status == TaskStatus.COMPLETED.value,
            TaskRecord.completed_at >= day_start,
            TaskRecord.completed_at < day_end,
        ]

    @staticmethod
    def _order_by(sort: TaskSort) -> tuple[Any, ...]:
        pinned_first = TaskRecord.is_pinned.desc()
        priority_rank = case(
            {
                TaskPriority.URGENT.value: 0,
                TaskPriority.IMPORTANT.value: 1,
                TaskPriority.ATTENTION.value: 2,
                TaskPriority.NORMAL.value: 3,
            },
            value=TaskRecord.priority,
            else_=4,
        )
        schedule = (TaskRecord.starts_at.is_(None), TaskRecord.starts_at.asc())
        if sort is TaskSort.PRIORITY:
            return (pinned_first, priority_rank, *schedule, TaskRecord.title, TaskRecord.id)
        if sort is TaskSort.UPDATED:
            return (pinned_first, TaskRecord.updated_at.desc(), TaskRecord.id.desc())
        if sort is TaskSort.TITLE:
            return (pinned_first, TaskRecord.title.collate("NOCASE"), TaskRecord.id)
        return (pinned_first, *schedule, priority_rank, TaskRecord.title, TaskRecord.id)

    @staticmethod
    def _to_record(task: Task) -> TaskRecord:
        record = TaskRecord()
        SqlAlchemyTaskRepository._copy_to_record(task, record)
        return record

    @staticmethod
    def _copy_to_record(task: Task, record: TaskRecord) -> None:
        record.legacy_id = task.legacy_id
        record.title = task.title
        record.description = task.description
        record.status = task.status.value
        record.priority = task.priority.value
        record.is_pinned = task.is_pinned
        record.all_day = task.all_day
        record.starts_at = task.starts_at
        record.ends_at = task.ends_at
        record.timezone = task.timezone
        record.recurrence_rule = task.recurrence_rule
        record.result_note = task.result_note
        record.completed_at = task.completed_at
        record.created_at = task.created_at
        record.updated_at = task.updated_at
        record.deleted_at = task.deleted_at

    @staticmethod
    def _to_domain(record: TaskRecord) -> Task:
        return Task(
            id=record.id,
            legacy_id=record.legacy_id,
            title=record.title,
            description=record.description,
            status=TaskStatus(record.status),
            priority=TaskPriority(record.priority),
            is_pinned=record.is_pinned,
            all_day=record.all_day,
            starts_at=record.starts_at,
            ends_at=record.ends_at,
            timezone=record.timezone,
            recurrence_rule=record.recurrence_rule,
            result_note=record.result_note,
            completed_at=record.completed_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
            deleted_at=record.deleted_at,
        )
