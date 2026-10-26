from __future__ import annotations

from sqlalchemy import select

from officeflow.application.recent_tasks import RecentTaskSummary
from officeflow.infrastructure.database.models import TaskRecord
from officeflow.infrastructure.database.session import SessionFactory


class SqlAlchemyRecentTaskRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def _read(self, ids: tuple[int, ...] | None) -> tuple[RecentTaskSummary, ...]:
        statement = select(
            TaskRecord.id,
            TaskRecord.title,
            TaskRecord.status,
            TaskRecord.created_at,
            TaskRecord.is_pinned,
        ).where(TaskRecord.deleted_at.is_(None))
        if ids is not None:
            statement = statement.where(TaskRecord.id.in_(ids[:20]))
        else:
            statement = (
                statement.where(TaskRecord.is_pinned.is_(True))
                .order_by(TaskRecord.updated_at.desc(), TaskRecord.id.desc())
                .limit(4)
            )
        with self._sessions.transaction() as session:
            return tuple(RecentTaskSummary(*row) for row in session.execute(statement))

    def summaries(self, ids: tuple[int, ...]) -> dict[int, RecentTaskSummary]:
        return {r.task_id: r for r in self._read(ids)} if ids else {}

    def pinned(self) -> tuple[RecentTaskSummary, ...]:
        return self._read(None)
