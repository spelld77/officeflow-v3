"""Persisted completion scopes. Aggregate in SQL, fetch at most 2 x 51 rows."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import (
    and_,
    case,
    func,
    literal,
    literal_column,
    or_,
    select,
    text,
    tuple_,
    union_all,
)
from sqlalchemy.orm import Session

from officeflow.application.completed_browse import (
    CompletedEntry,
    CompletedEntryKey,
    CompletionBucket,
    CompletionCursor,
    CompletionPage,
    CompletionRequest,
    CompletionResult,
    Month,
    month_bounds,
    month_shift,
)
from officeflow.domain.enums import TaskStatus
from officeflow.infrastructure.database.models import (
    TaskOccurrenceRecord,
    TaskRecord,
    WorkLogRecord,
)
from officeflow.infrastructure.database.search import fts_prefix_query, matching_attachment_tasks
from officeflow.infrastructure.database.session import SessionFactory
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository


class SqlAlchemyCompletedBrowseRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self.sessions = sessions

    @staticmethod
    def keys(request: CompletionRequest) -> Any:
        query = request.query
        shared: list[Any] = [TaskRecord.deleted_at.is_(None)]
        if query.statuses and TaskStatus.COMPLETED not in query.statuses:
            shared.append(literal(False))
        if query.priorities:
            shared.append(TaskRecord.priority.in_([v.value for v in query.priorities]))
        if query.pinned_only:
            shared.append(TaskRecord.is_pinned.is_(True))
        if query.has_attachments is not None:
            attached = SqlAlchemyTaskRepository._attachment_exists()
            shared.append(attached if query.has_attachments else ~attached)
        regular = [*shared, TaskRecord.status == "completed"]
        recurring = [*shared, TaskOccurrenceRecord.status == "completed"]
        for predicates, column in (
            (regular, TaskRecord.completed_at),
            (recurring, TaskOccurrenceRecord.completed_at),
        ):
            if query.completed_after is not None:
                predicates.append(column >= query.completed_after)
            if query.completed_before is not None:
                predicates.append(column < query.completed_before)
        search = query.search.strip()
        if search:
            regular.append(SqlAlchemyTaskRepository._search_predicate(search))
            content = func.lower(
                TaskRecord.title
                + literal("\n")
                + TaskRecord.description
                + literal("\n")
                + TaskOccurrenceRecord.result_note
            )
            log_matches: Any = (
                select(literal_column("rowid"))
                .select_from(text("work_log_search"))
                .where(
                    text("work_log_search MATCH :completion_fts").bindparams(
                        completion_fts=fts_prefix_query(search)
                    )
                )
            )
            recurring.append(
                or_(
                    and_(*(func.instr(content, term.lower()) > 0 for term in search.split())),
                    TaskRecord.id.in_(matching_attachment_tasks(search)),
                    select(WorkLogRecord.id)
                    .where(
                        WorkLogRecord.occurrence_id == TaskOccurrenceRecord.id,
                        WorkLogRecord.id.in_(log_matches),
                    )
                    .exists(),
                )
            )
        return union_all(
            select(
                TaskRecord.id.label("task_id"),
                literal(0).label("occurrence_id"),
                literal(0).label("scope_rank"),
                TaskRecord.completed_at.label("completed_at"),
            ).where(*regular),
            select(
                TaskOccurrenceRecord.task_id,
                TaskOccurrenceRecord.id,
                literal(1),
                TaskOccurrenceRecord.completed_at,
            )
            .join(TaskRecord, TaskRecord.id == TaskOccurrenceRecord.task_id)
            .where(*recurring),
        ).cte("completions")

    def read(self, request: CompletionRequest) -> CompletionResult:
        with self.sessions.read_snapshot() as snapshot, snapshot.transaction() as session:
            return self._read(session, request)

    def _read(self, session: Session, request: CompletionRequest) -> CompletionResult:
        keys = self.keys(request)
        total, unknown = session.execute(
            select(func.count(), func.count() - func.count(keys.c.completed_at)).select_from(keys)
        ).one()
        latest_statement = select(func.max(keys.c.completed_at))
        if request.before is not None:
            latest_statement = latest_statement.where(keys.c.completed_at < request.before)
        latest: datetime | None = session.scalar(latest_statement)
        buckets: list[CompletionBucket] = []
        oldest: datetime | None = None
        if request.flat:
            buckets = [CompletionBucket(None, int(total))] if total else []
        elif latest is not None:
            local = latest.astimezone(ZoneInfo(request.timezone))
            months = [month_shift(local.year, local.month, -i) for i in range(6)]
            bounds = [month_bounds(month, request.timezone) for month in months]
            counts = session.execute(
                select(
                    *[
                        func.count(
                            case((and_(keys.c.completed_at >= start, keys.c.completed_at < end), 1))
                        )
                        for start, end in bounds
                    ]
                ).select_from(keys)
            ).one()
            buckets = [
                CompletionBucket(month, int(count))
                for month, count in zip(months, counts, strict=True)
                if count
            ]
            oldest = bounds[-1][0]
        if not request.flat and unknown:
            buckets.append(CompletionBucket(None, int(unknown)))
        expanded = (
            request.expanded
            if request.expanded is not None
            else ((buckets[0].month,) if buckets else ())
        )
        cursor_map = dict(request.cursors)
        pages = tuple(
            self._page(session, keys, request, bucket.month, cursor_map.get(bucket.month))
            for bucket in buckets
            if bucket.month in expanded[-2:]
        )
        has_older = (
            oldest is not None
            and session.scalar(select(keys.c.task_id).where(keys.c.completed_at < oldest).limit(1))
            is not None
        )
        return CompletionResult(
            int(total),
            tuple(buckets),
            pages,
            oldest,
            has_older,
            sum(b.count for b in buckets if b.month is not None),
        )

    def _page(
        self,
        session: Session,
        keys: Any,
        request: CompletionRequest,
        month: Month,
        cursor: CompletionCursor | None,
    ) -> CompletionPage:
        predicates: list[Any] = []
        if not request.flat:
            if month is None:
                predicates.append(keys.c.completed_at.is_(None))
            else:
                start, end = month_bounds(month, request.timezone)
                predicates.extend((keys.c.completed_at >= start, keys.c.completed_at < end))
        order = (
            keys.c.completed_at.desc(),
            keys.c.task_id.desc(),
            keys.c.scope_rank.desc(),
            keys.c.occurrence_id.desc(),
        )
        if cursor is not None:
            ids = tuple_(keys.c.task_id, keys.c.scope_rank, keys.c.occurrence_id)
            values = tuple_(
                literal(cursor.task_id), literal(cursor.scope_rank), literal(cursor.occurrence_id)
            )
            if cursor.completed_at is None:
                predicates.extend((keys.c.completed_at.is_(None), ids < values))
            else:
                older = tuple_(
                    keys.c.completed_at, keys.c.task_id, keys.c.scope_rank, keys.c.occurrence_id
                ) < tuple_(
                    literal(cursor.completed_at),
                    literal(cursor.task_id),
                    literal(cursor.scope_rank),
                    literal(cursor.occurrence_id),
                )
                predicates.append(
                    or_(older, keys.c.completed_at.is_(None)) if request.flat else older
                )
        rows = session.execute(
            select(
                keys,
                TaskRecord.title,
                TaskRecord.recurrence_rule,
                TaskOccurrenceRecord.occurrence_start,
            )
            .join(TaskRecord, TaskRecord.id == keys.c.task_id)
            .outerjoin(
                TaskOccurrenceRecord,
                and_(keys.c.scope_rank == 1, TaskOccurrenceRecord.id == keys.c.occurrence_id),
            )
            .where(*predicates)
            .order_by(*order)
            .limit(51)
        ).all()
        entries = tuple(
            CompletedEntry(
                CompletedEntryKey(
                    row.task_id, row.occurrence_start, "occurrence" if row.scope_rank else "task"
                ),
                row.title,
                row.completed_at,
                bool(row.recurrence_rule),
                CompletionCursor(row.completed_at, row.task_id, row.scope_rank, row.occurrence_id),
            )
            for row in rows[:50]
        )
        return CompletionPage(month, entries, entries[-1].cursor if len(rows) > 50 else None)
