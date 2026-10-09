from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, and_, literal, or_, select, union_all

from officeflow.application.task_history import HistoryQuery
from officeflow.application.tasks import TaskService
from officeflow.domain.task_history import (
    HistoryEntry,
    HistoryPage,
    HistorySummary,
    encode,
    task_values,
    utc_text,
)
from officeflow.infrastructure.database.models import (
    SnapshotFileRecord,
    TaskHistoryRecord,
    TaskSnapshotRecord,
)
from officeflow.infrastructure.database.session import SessionFactory
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository


class HistoryWriter:
    def __init__(self, sessions: SessionFactory) -> None:
        self.sessions = sessions

    def append(self, **values: Any) -> int:
        with self.sessions.transaction() as session:
            record = TaskHistoryRecord(**values)
            session.add(record)
            session.flush()
            return record.id


class SqlAlchemyTaskHistoryRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def subscribe(self, callback: Callable[[int], None]) -> Callable[[], None]:
        return self._sessions.task_events.subscribe(callback)

    def page(self, task_id: int, query: HistoryQuery) -> HistoryPage:
        record = TaskHistoryRecord
        # Only summary columns: never materialize large before/after text for lists.
        history = select(
            record.id,
            record.task_id,
            record.scope_key,
            record.occurrence_start,
            record.event_type,
            record.recorded_at,
            record.changed_fields,
            record.summary,
            literal("history").label("kind"),
            literal(0).label("rank"),
            literal(0).label("file_count"),
        ).where(record.task_id == task_id)
        snapshot = TaskSnapshotRecord
        saved = select(
            snapshot.id,
            snapshot.task_id,
            snapshot.scope_key,
            snapshot.occurrence_start,
            literal("snapshot").label("event_type"),
            snapshot.captured_at.label("recorded_at"),
            literal("[]").label("changed_fields"),
            snapshot.summary,
            literal("snapshot").label("kind"),
            literal(1).label("rank"),
            snapshot.file_count,
        ).where(snapshot.task_id == task_id)
        # Filtering/seek/limit must happen BEFORE union. Sorting all 100k metadata
        # rows just to render 50 defeats the indexed keyset page budget.
        timeline = union_all(
            self._bounded(history, query, 0), self._bounded(saved, query, 1)
        ).subquery()
        columns = timeline.c
        statement = (
            select(timeline)
            .order_by(columns.recorded_at.desc(), columns.rank.desc(), columns.id.desc())
            .limit(query.limit + 1)
        )
        with self._sessions.read_snapshot() as sessions, sessions.transaction() as session:
            rows = session.execute(statement).all()
        items = tuple(
            HistorySummary(
                id=row.id,
                task_id=row.task_id,
                scope_key=row.scope_key,
                occurrence_start=row.occurrence_start,
                event_type=row.event_type,
                recorded_at=row.recorded_at,
                changed_fields=tuple(json.loads(row.changed_fields)),
                summary=row.summary,
                kind=row.kind,
                file_count=row.file_count,
            )
            for row in rows[: query.limit]
        )
        return HistoryPage(items, len(rows) > query.limit)

    @staticmethod
    def _bounded(statement: Select[Any], query: HistoryQuery, branch_rank: int) -> Select[Any]:
        columns = statement.selected_columns
        if query.scope_keys:
            statement = statement.where(columns.scope_key.in_(query.scope_keys))
        if query.event_type:
            statement = statement.where(columns.event_type == query.event_type)
        if query.changed_field:
            statement = statement.where(
                columns.changed_fields.contains(encode(query.changed_field))
            )
        if query.after is not None:
            statement = statement.where(columns.recorded_at >= query.after)
        if query.before is not None:
            statement = statement.where(columns.recorded_at < query.before)
        if query.cursor:
            when, rank, identity = query.cursor
            if branch_rank < rank:
                statement = statement.where(columns.recorded_at <= when)
            elif branch_rank > rank:
                statement = statement.where(columns.recorded_at < when)
            else:
                statement = statement.where(columns.recorded_at <= when).where(
                    or_(
                        columns.recorded_at < when,
                        and_(columns.recorded_at == when, columns.id < identity),
                    )
                )
        statement = statement.order_by(columns.recorded_at.desc(), columns.id.desc()).limit(
            query.limit + 1
        )
        return select(statement.subquery())

    def get(self, task_id: int, entry_id: int, kind: str = "history") -> HistoryEntry:
        if kind == "snapshot":
            with self._sessions.transaction() as session:
                saved = session.get(TaskSnapshotRecord, entry_id)
                if saved is None or saved.task_id != task_id or saved.payload_version != 1:
                    raise LookupError("중요 보관을 찾을 수 없습니다.")
                info = json.loads(saved.info_payload)
                info["captured_at"] = utc_text(saved.captured_at)
                info["committed_at"] = utc_text(saved.committed_at)
                info["files"] = [
                    dict(
                        name=row.name,
                        role=row.role,
                        memo=row.memo,
                        size=row.fixed_size_bytes,
                        checksum=row.fixed_checksum,
                    )
                    for row in session.execute(
                        select(
                            SnapshotFileRecord.name,
                            SnapshotFileRecord.role,
                            SnapshotFileRecord.memo,
                            SnapshotFileRecord.fixed_size_bytes,
                            SnapshotFileRecord.fixed_checksum,
                        )
                        .where(SnapshotFileRecord.snapshot_id == entry_id)
                        .order_by(SnapshotFileRecord.position)
                    )
                ]
                return HistoryEntry(
                    HistorySummary(
                        saved.id,
                        saved.task_id,
                        saved.scope_key,
                        saved.occurrence_start,
                        "snapshot",
                        saved.captured_at,
                        (),
                        saved.label,
                        "snapshot",
                        saved.file_count,
                    ),
                    saved.job_id,
                    None,
                    info["values"],
                    info,
                )
        if kind != "history":
            raise ValueError("지원하지 않는 이력 종류입니다.")
        with self._sessions.transaction() as session:
            row = session.scalar(
                select(TaskHistoryRecord).where(
                    TaskHistoryRecord.id == entry_id, TaskHistoryRecord.task_id == task_id
                )
            )
            if row is None:
                raise LookupError("이력을 찾을 수 없습니다. 다시 조회하세요.")
            if row.payload_version != 1:
                raise ValueError("지원하지 않는 이력 형식입니다.")
            return HistoryEntry(
                HistorySummary(
                    row.id,
                    row.task_id,
                    row.scope_key,
                    row.occurrence_start,
                    row.event_type,
                    row.recorded_at,
                    tuple(json.loads(row.changed_fields)),
                    row.summary,
                ),
                row.command_id,
                json.loads(row.before_payload) if row.before_payload else None,
                json.loads(row.after_payload),
            )

    def current(self, task_id: int, occurrence_start: datetime | None) -> dict[str, Any]:
        with self._sessions.read_snapshot() as sessions:
            tasks = TaskService(SqlAlchemyTaskRepository(sessions))
            task = tasks.get_including_deleted(task_id)
            occurrence = tasks.occurrence(task_id, occurrence_start) if occurrence_start else None
            if occurrence_start and occurrence is None:
                occurrence = tasks._validated_occurrence(task, occurrence_start)
            return task_values(task, occurrence)

    def compare(
        self, task_id: int, left: tuple[str, int], right: tuple[str, int]
    ) -> tuple[HistoryEntry, HistoryEntry]:
        with self._sessions.read_snapshot() as sessions:
            repository = SqlAlchemyTaskHistoryRepository(sessions)
            return repository.get(task_id, left[1], left[0]), repository.get(
                task_id, right[1], right[0]
            )

    def compare_current(
        self, task_id: int, identity: tuple[str, int]
    ) -> tuple[HistoryEntry, dict[str, Any], datetime]:
        with self._sessions.read_snapshot() as sessions:
            repository = SqlAlchemyTaskHistoryRepository(sessions)
            entry = repository.get(task_id, identity[1], identity[0])
            values = repository.current(task_id, entry.summary.occurrence_start)
            return entry, values, datetime.now(UTC)
