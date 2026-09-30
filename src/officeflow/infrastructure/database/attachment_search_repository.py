from __future__ import annotations

import sqlite3
from collections.abc import Callable
from time import monotonic
from typing import cast

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import OperationalError

from officeflow.application.attachment_search import (
    AttachmentCursor,
    AttachmentKind,
    AttachmentSearchHit,
    AttachmentSearchInterrupted,
    AttachmentSearchPage,
    AttachmentSearchQuery,
)
from officeflow.domain.enums import TaskStatus
from officeflow.infrastructure.database.models import AttachmentRecord, TaskRecord
from officeflow.infrastructure.database.search import attachment_name_predicate
from officeflow.infrastructure.database.session import SessionFactory

EXTENSIONS = {
    AttachmentKind.EXCEL: (".xlsx", ".xls", ".xlsm", ".xlsb", ".csv"),
    AttachmentKind.PDF: (".pdf",),
    AttachmentKind.WORD: (".doc", ".docx", ".docm", ".rtf"),
    AttachmentKind.IMAGE: (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"),
}


class SqlAlchemyAttachmentSearchRepository:
    def __init__(self, sessions: SessionFactory, *, timeout_seconds: float = 3.0) -> None:
        self._sessions = sessions
        self._timeout_seconds = timeout_seconds

    def search_page(
        self,
        query: AttachmentSearchQuery,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AttachmentSearchPage:
        deadline = monotonic() + self._timeout_seconds

        def interrupted() -> bool:
            return bool(
                (cancel_requested is not None and cancel_requested()) or monotonic() > deadline
            )

        if interrupted():
            raise AttachmentSearchInterrupted("검색이 취소되었거나 시간이 초과되었습니다.")
        statement = select(
            AttachmentRecord.id,
            AttachmentRecord.task_id,
            AttachmentRecord.original_name,
            AttachmentRecord.size_bytes,
            AttachmentRecord.created_at,
            AttachmentRecord.missing_at,
            AttachmentRecord.detached_at,
            TaskRecord.title,
            TaskRecord.status,
            TaskRecord.deleted_at,
        ).join(TaskRecord, AttachmentRecord.task_id == TaskRecord.id)
        if not query.include_trash:
            statement = statement.where(TaskRecord.deleted_at.is_(None))
        if not query.include_detached:
            statement = statement.where(AttachmentRecord.detached_at.is_(None))
        if query.search.strip():
            statement = statement.where(attachment_name_predicate(query.search))
        if query.attached_after is not None:
            statement = statement.where(AttachmentRecord.created_at >= query.attached_after)
        if query.attached_before is not None:
            statement = statement.where(AttachmentRecord.created_at < query.attached_before)
        if query.kind is not AttachmentKind.ALL:
            suffixes = (
                tuple(suffix for group in EXTENSIONS.values() for suffix in group)
                if query.kind is AttachmentKind.OTHER
                else EXTENSIONS[query.kind]
            )
            matches = or_(
                *(
                    func.substr(func.lower(AttachmentRecord.original_name), -len(suffix)) == suffix
                    for suffix in suffixes
                )
            )
            statement = statement.where(~matches if query.kind is AttachmentKind.OTHER else matches)
        if query.cursor is not None:
            statement = statement.where(
                or_(
                    AttachmentRecord.created_at < query.cursor.created_at,
                    and_(
                        AttachmentRecord.created_at == query.cursor.created_at,
                        AttachmentRecord.id < query.cursor.attachment_id,
                    ),
                )
            )
        statement = statement.order_by(
            AttachmentRecord.created_at.desc(), AttachmentRecord.id.desc()
        ).limit(query.limit + 1)
        with self._sessions.transaction() as session:
            connection = cast(sqlite3.Connection, session.connection().connection.driver_connection)
            connection.set_progress_handler(lambda: int(interrupted()), 1000)
            try:
                rows = session.execute(statement).all()
                if interrupted():
                    raise AttachmentSearchInterrupted(
                        "검색이 완료되지 않았습니다. 검색어를 더 구체적으로 입력해 주세요."
                    )
            except OperationalError as error:
                if interrupted():
                    raise AttachmentSearchInterrupted(
                        "검색이 완료되지 않았습니다. 검색어를 더 구체적으로 입력해 주세요."
                    ) from error
                raise
            finally:
                connection.set_progress_handler(None, 0)
        items = tuple(
            AttachmentSearchHit(
                attachment_id=row.id,
                task_id=row.task_id,
                original_name=row.original_name,
                size_bytes=row.size_bytes,
                created_at=row.created_at,
                task_title=row.title,
                task_status=TaskStatus(row.status),
                deleted=row.deleted_at is not None,
                detached=row.detached_at is not None,
                missing=row.missing_at is not None,
            )
            for row in rows[: query.limit]
        )
        more = len(rows) > query.limit
        cursor = AttachmentCursor(items[-1].created_at, items[-1].attachment_id) if items else None
        return AttachmentSearchPage(items, cursor, more)
