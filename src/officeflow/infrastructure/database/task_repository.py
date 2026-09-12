from __future__ import annotations

from sqlalchemy import Select, select

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

    def list(self, *, search: str = "") -> list[Task]:
        statement: Select[tuple[TaskRecord]] = select(TaskRecord).where(
            TaskRecord.deleted_at.is_(None)
        )
        normalized = search.strip()
        if normalized:
            escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            statement = statement.where(
                TaskRecord.title.ilike(pattern, escape="\\")
                | TaskRecord.description.ilike(pattern, escape="\\")
            )
        with self._sessions.transaction() as session:
            return [self._to_domain(record) for record in session.scalars(statement).all()]

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
