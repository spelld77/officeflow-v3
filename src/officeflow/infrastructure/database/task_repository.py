from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Select,
    and_,
    case,
    func,
    literal,
    literal_column,
    or_,
    select,
    text,
    union_all,
)

from officeflow.application.tasks import (
    CalendarRepositoryOverview,
    TaskGroup,
    TaskPage,
    TaskQuery,
    TaskSort,
    TaskView,
)
from officeflow.domain.enums import OccurrenceStatus, TaskPriority, TaskStatus
from officeflow.domain.occurrence import TaskOccurrence
from officeflow.domain.recurrence import recurrence_until_utc
from officeflow.domain.task import Task
from officeflow.infrastructure.database.models import (
    AttachmentRecord,
    TaskOccurrenceRecord,
    TaskRecord,
)
from officeflow.infrastructure.database.search import fts_prefix_query
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
        attachment_exists = self._attachment_exists()
        statement = select(TaskRecord, attachment_exists).where(TaskRecord.id == task_id)
        with self._sessions.transaction() as session:
            row = session.execute(statement).one_or_none()
            if row is None or row[0].deleted_at is not None:
                return None
            return self._to_domain(row[0], has_attachments=bool(row[1]))

    def get_deleted(self, task_id: int) -> Task | None:
        attachment_exists = self._attachment_exists()
        statement = select(TaskRecord, attachment_exists).where(TaskRecord.id == task_id)
        with self._sessions.transaction() as session:
            row = session.execute(statement).one_or_none()
            if row is None or row[0].deleted_at is None:
                return None
            return self._to_domain(row[0], has_attachments=bool(row[1]))

    def get_many(
        self,
        task_ids: tuple[int, ...],
        *,
        include_deleted: bool = False,
    ) -> dict[int, Task]:
        if not task_ids:
            return {}
        attachment_exists = self._attachment_exists()
        statement = select(TaskRecord, attachment_exists).where(
            TaskRecord.id.in_(task_ids)
        )
        if not include_deleted:
            statement = statement.where(TaskRecord.deleted_at.is_(None))
        with self._sessions.transaction() as session:
            return {
                record.id: self._to_domain(
                    record,
                    has_attachments=bool(has_attachments),
                )
                for record, has_attachments in session.execute(statement).all()
            }

    def soft_delete(self, task_id: int, *, deleted_at: datetime) -> Task:
        with self._sessions.transaction() as session:
            record = session.get(TaskRecord, task_id)
            if record is None or record.deleted_at is not None:
                raise LookupError(f"업무 {task_id}을(를) 찾을 수 없습니다.")
            record.deleted_at = deleted_at
            record.updated_at = deleted_at
        deleted = self.get_deleted(task_id)
        if deleted is None:
            raise LookupError(f"업무 {task_id}을(를) 찾을 수 없습니다.")
        return deleted

    def restore(self, task_id: int, *, restored_at: datetime) -> Task:
        with self._sessions.transaction() as session:
            record = session.get(TaskRecord, task_id)
            if record is None or record.deleted_at is None:
                raise LookupError(f"휴지통에서 업무 {task_id}을(를) 찾을 수 없습니다.")
            record.deleted_at = None
            record.updated_at = restored_at
        return self.get_required(task_id)

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
        attachment_exists = self._attachment_exists()
        statement: Select[tuple[TaskRecord, bool]] = (
            select(TaskRecord, attachment_exists)
            .where(*predicates)
            .order_by(*self._order_by(query.sort))
        )
        if query.offset:
            statement = statement.offset(query.offset)
        if query.limit is not None:
            statement = statement.limit(query.limit)
        with self._sessions.transaction() as session:
            total = int(session.scalar(count_statement) or 0)
            items = tuple(
                self._to_domain(record, has_attachments=bool(has_attachments))
                for record, has_attachments in session.execute(statement).all()
            )
        return TaskPage(items=items, total=total, offset=query.offset, limit=query.limit)

    def list_overlapping(
        self,
        starts_at: datetime,
        ends_at: datetime,
        *,
        search: str = "",
    ) -> tuple[Task, ...]:
        if ends_at <= starts_at:
            raise ValueError("캘린더 조회 종료 시각은 시작 시각보다 늦어야 합니다.")
        predicates: list[Any] = [
            TaskRecord.deleted_at.is_(None),
            TaskRecord.status != TaskStatus.ARCHIVED.value,
            TaskRecord.starts_at.is_not(None),
            TaskRecord.starts_at < ends_at,
            or_(
                and_(
                    TaskRecord.recurrence_rule.is_not(None),
                    or_(
                        TaskRecord.recurrence_until.is_(None),
                        TaskRecord.recurrence_until >= starts_at,
                    ),
                ),
                and_(
                    TaskRecord.recurrence_rule.is_(None),
                    or_(
                        TaskRecord.ends_at > starts_at,
                        and_(TaskRecord.ends_at.is_(None), TaskRecord.starts_at >= starts_at),
                    ),
                ),
            ),
        ]
        normalized = search.strip()
        if normalized:
            predicates.append(self._search_predicate(normalized))
        attachment_exists = self._attachment_exists()
        statement = (
            select(TaskRecord, attachment_exists)
            .where(*predicates)
            .order_by(
                TaskRecord.is_pinned.desc(),
                TaskRecord.starts_at.asc(),
                TaskRecord.ends_at.asc(),
                TaskRecord.id.asc(),
            )
        )
        with self._sessions.transaction() as session:
            return tuple(
                self._to_domain(record, has_attachments=bool(has_attachments))
                for record, has_attachments in session.execute(statement).all()
            )

    def calendar_overview(
        self,
        starts_at: datetime,
        ends_at: datetime,
        day_ranges: tuple[tuple[datetime, datetime], ...],
        *,
        search: str = "",
        preview_limit: int = 60,
    ) -> CalendarRepositoryOverview:
        if ends_at <= starts_at or not day_ranges:
            raise ValueError("캘린더 조회 범위가 올바르지 않습니다.")
        common: list[Any] = [
            TaskRecord.deleted_at.is_(None),
            TaskRecord.status != TaskStatus.ARCHIVED.value,
            TaskRecord.starts_at.is_not(None),
        ]
        normalized = search.strip()
        if normalized:
            common.append(self._search_predicate(normalized))
        regular = [
            *common,
            TaskRecord.recurrence_rule.is_(None),
            TaskRecord.starts_at < ends_at,
            or_(
                TaskRecord.ends_at > starts_at,
                and_(TaskRecord.ends_at.is_(None), TaskRecord.starts_at >= starts_at),
            ),
        ]
        attachment_exists = self._attachment_exists()
        preview_statement = (
            select(TaskRecord, attachment_exists)
            .where(*regular)
            .order_by(
                TaskRecord.is_pinned.desc(),
                TaskRecord.starts_at.asc(),
                TaskRecord.ends_at.asc(),
                TaskRecord.id.asc(),
            )
            .limit(preview_limit)
        )
        count_statements = [
            select(
                literal(index).label("day_index"),
                func.count(TaskRecord.id).label("total"),
            ).where(
                *common,
                TaskRecord.recurrence_rule.is_(None),
                TaskRecord.starts_at < day_end,
                or_(
                    TaskRecord.ends_at > day_start,
                    and_(
                        TaskRecord.ends_at.is_(None),
                        TaskRecord.starts_at >= day_start,
                    ),
                ),
            )
            for index, (day_start, day_end) in enumerate(day_ranges)
        ]
        recurrence_statement = (
            select(TaskRecord, attachment_exists)
            .where(
                *common,
                TaskRecord.recurrence_rule.is_not(None),
                TaskRecord.starts_at < ends_at,
                or_(
                    TaskRecord.recurrence_until.is_(None),
                    TaskRecord.recurrence_until >= starts_at,
                ),
            )
            .order_by(TaskRecord.starts_at, TaskRecord.id)
        )
        with self._sessions.transaction() as session:
            regular_total = int(session.scalar(select(func.count(TaskRecord.id)).where(*regular)) or 0)
            regular_previews = tuple(
                self._to_domain(record, has_attachments=bool(has_attachments))
                for record, has_attachments in session.execute(preview_statement).all()
            )
            count_rows = session.execute(union_all(*count_statements)).all()
            recurrence_templates = tuple(
                self._to_domain(record, has_attachments=bool(has_attachments))
                for record, has_attachments in session.execute(recurrence_statement).all()
            )
        counts = [0] * len(day_ranges)
        for day_index, total in count_rows:
            counts[int(day_index)] = int(total)
        return CalendarRepositoryOverview(
            regular_previews=regular_previews,
            regular_day_counts=tuple(counts),
            regular_total=regular_total,
            recurrence_templates=recurrence_templates,
        )

    def get_occurrence(self, task_id: int, occurrence_start: datetime) -> TaskOccurrence | None:
        statement = select(TaskOccurrenceRecord).where(
            TaskOccurrenceRecord.task_id == task_id,
            TaskOccurrenceRecord.occurrence_start == occurrence_start,
        )
        with self._sessions.transaction() as session:
            record = session.scalar(statement)
            return self._occurrence_to_domain(record) if record is not None else None

    def get_occurrence_by_id(self, occurrence_id: int) -> TaskOccurrence | None:
        with self._sessions.transaction() as session:
            record = session.get(TaskOccurrenceRecord, occurrence_id)
            return self._occurrence_to_domain(record) if record is not None else None

    def save_occurrence(self, occurrence: TaskOccurrence) -> TaskOccurrence:
        statement = select(TaskOccurrenceRecord).where(
            TaskOccurrenceRecord.task_id == occurrence.task_id,
            TaskOccurrenceRecord.occurrence_start == occurrence.occurrence_start,
        )
        with self._sessions.transaction() as session:
            record = session.scalar(statement)
            if record is None:
                record = TaskOccurrenceRecord(
                    task_id=occurrence.task_id,
                    occurrence_start=occurrence.occurrence_start,
                )
                session.add(record)
            record.occurrence_end = occurrence.occurrence_end
            record.effective_start = occurrence.effective_start
            record.effective_end = occurrence.effective_end
            record.status = occurrence.status.value
            record.completed_at = occurrence.completed_at
            record.result_note = occurrence.result_note
            session.flush()
            occurrence_id = record.id
        saved = self.get_occurrence(occurrence.task_id, occurrence.occurrence_start)
        if saved is None:
            raise LookupError(f"반복 발생 건 {occurrence_id}을(를) 찾을 수 없습니다.")
        return saved

    def list_occurrences(
        self,
        task_ids: tuple[int, ...],
        starts_at: datetime,
        ends_at: datetime,
    ) -> tuple[TaskOccurrence, ...]:
        if not task_ids:
            return ()
        statement = (
            select(TaskOccurrenceRecord)
            .where(
                TaskOccurrenceRecord.task_id.in_(task_ids),
                TaskOccurrenceRecord.occurrence_start >= starts_at,
                TaskOccurrenceRecord.occurrence_start < ends_at,
            )
            .order_by(TaskOccurrenceRecord.occurrence_start, TaskOccurrenceRecord.id)
        )
        with self._sessions.transaction() as session:
            return tuple(
                self._occurrence_to_domain(record) for record in session.scalars(statement).all()
            )

    @classmethod
    def _predicates(
        cls,
        query: TaskQuery,
        *,
        current: datetime,
        day_start: datetime,
        day_end: datetime,
    ) -> list[Any]:
        predicates: list[Any] = [
            TaskRecord.deleted_at.is_not(None)
            if query.view is TaskView.TRASH
            else TaskRecord.deleted_at.is_(None)
        ]
        if query.group is None:
            predicates.extend(cls._view_predicates(query.view, day_start, day_end))
        else:
            predicates.extend(cls._group_predicates(query.group, current, day_start, day_end))

        normalized = query.search.strip()
        if normalized:
            predicates.append(cls._search_predicate(normalized))
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
        if query.has_attachments is not None:
            attachment_exists = cls._attachment_exists()
            predicates.append(
                attachment_exists if query.has_attachments else ~attachment_exists
            )
        if query.completed_after is not None:
            predicates.append(TaskRecord.completed_at >= query.completed_after)
        if query.completed_before is not None:
            predicates.append(TaskRecord.completed_at < query.completed_before)
        return predicates

    @staticmethod
    def _search_predicate(search: str) -> Any:
        query = fts_prefix_query(search)
        matching_tasks: Any = (
            select(literal_column("rowid"))
            .select_from(text("task_search"))
            .where(text("task_search MATCH :task_fts").bindparams(task_fts=query))
        )
        matching_work_log_tasks: Any = (
            select(literal_column("task_id"))
            .select_from(text("work_log_search"))
            .where(
                text("work_log_search MATCH :work_log_fts").bindparams(
                    work_log_fts=query
                )
            )
        )
        return or_(
            TaskRecord.id.in_(matching_tasks),
            TaskRecord.id.in_(matching_work_log_tasks),
        )

    @staticmethod
    def _attachment_exists() -> Any:
        return (
            select(AttachmentRecord.id)
            .where(
                AttachmentRecord.task_id == TaskRecord.id,
                AttachmentRecord.detached_at.is_(None),
            )
            .exists()
        )

    @staticmethod
    def _view_predicates(view: TaskView, day_start: datetime, day_end: datetime) -> list[Any]:
        active = (TaskStatus.ACTIVE.value, TaskStatus.PENDING.value)
        if view is TaskView.TODAY:
            return [
                TaskRecord.recurrence_rule.is_(None),
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
        if view is TaskView.TRASH:
            return []
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
                TaskRecord.recurrence_rule.is_(None),
                TaskRecord.status.in_(active),
                TaskRecord.starts_at.is_not(None),
                or_(
                    TaskRecord.ends_at <= current,
                    and_(TaskRecord.ends_at.is_(None), TaskRecord.starts_at < current),
                ),
            ]
        if group is TaskGroup.IN_PROGRESS:
            return [
                TaskRecord.recurrence_rule.is_(None),
                TaskRecord.status.in_(active),
                TaskRecord.starts_at <= current,
                TaskRecord.ends_at > current,
            ]
        if group is TaskGroup.UPCOMING:
            return [
                TaskRecord.recurrence_rule.is_(None),
                TaskRecord.status.in_(active),
                or_(
                    TaskRecord.starts_at > current,
                    and_(TaskRecord.starts_at == current, TaskRecord.ends_at.is_(None)),
                ),
                TaskRecord.starts_at < day_end,
            ]
        return [
            TaskRecord.recurrence_rule.is_(None),
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
        record.recurrence_until = recurrence_until_utc(task.recurrence_rule)
        record.result_note = task.result_note
        record.completed_at = task.completed_at
        record.created_at = task.created_at
        record.updated_at = task.updated_at
        record.deleted_at = task.deleted_at

    @staticmethod
    def _to_domain(record: TaskRecord, *, has_attachments: bool = False) -> Task:
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
            has_attachments=has_attachments,
        )

    @staticmethod
    def _occurrence_to_domain(record: TaskOccurrenceRecord) -> TaskOccurrence:
        return TaskOccurrence(
            id=record.id,
            task_id=record.task_id,
            occurrence_start=record.occurrence_start,
            occurrence_end=record.occurrence_end,
            effective_start=record.effective_start,
            effective_end=record.effective_end,
            status=OccurrenceStatus(record.status),
            completed_at=record.completed_at,
            result_note=record.result_note,
        )
