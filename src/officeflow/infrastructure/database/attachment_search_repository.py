from __future__ import annotations

import sqlite3
from collections.abc import Callable
from time import monotonic
from typing import cast

from sqlalchemy import and_, func, literal, or_, select, union_all
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql.selectable import Subquery

from officeflow.application.attachment_search import (
    AttachmentCursor,
    AttachmentKind,
    AttachmentSearchHit,
    AttachmentSearchInterrupted,
    AttachmentSearchPage,
    AttachmentSearchQuery,
)
from officeflow.domain.enums import TaskStatus
from officeflow.infrastructure.database.models import (
    AttachmentRecord as A,
)
from officeflow.infrastructure.database.models import (
    AttachmentVersionRecord as V,
)
from officeflow.infrastructure.database.models import (
    FileAssetRecord as F,
)
from officeflow.infrastructure.database.models import (
    SnapshotFileRecord as S,
)
from officeflow.infrastructure.database.models import (
    TaskRecord as T,
)
from officeflow.infrastructure.database.models import (
    TaskSnapshotRecord as P,
)
from officeflow.infrastructure.database.session import SessionFactory

EXTENSIONS = {
    AttachmentKind.EXCEL: (".xlsx", ".xls", ".xlsm", ".xlsb", ".csv"),
    AttachmentKind.PDF: (".pdf",),
    AttachmentKind.WORD: (".doc", ".docx", ".docm", ".rtf"),
    AttachmentKind.IMAGE: (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"),
}


def document_search_rows() -> Subquery:
    """Logical IDs stay separate even when they share a frozen asset."""
    current = (
        select(
            A.id.label("id"),
            A.task_id,
            A.display_name.label("name"),
            A.original_name,
            func.coalesce(F.fixed_size_bytes, F.observed_size_bytes, A.size_bytes).label(
                "size_bytes"
            ),
            F.observed_at,
            A.created_at,
            F.missing_at,
            A.detached_at,
            literal("attachment").label("source_kind"),
            literal("").label("label"),
            A.memo,
        )
        .join(F, A.asset_id == F.id)
        .where(F.state == "active")
    )
    versions = (
        select(
            V.id,
            V.task_id,
            V.original_name,
            V.original_name,
            F.fixed_size_bytes,
            F.observed_at,
            V.created_at,
            F.missing_at,
            literal(None),
            literal("version"),
            V.label,
            V.memo,
        )
        .join(F, V.asset_id == F.id)
        .where(F.state == "active")
    )
    snapshots = (
        select(
            S.id,
            P.task_id,
            S.name,
            S.original_name,
            S.fixed_size_bytes,
            F.observed_at,
            P.captured_at,
            F.missing_at,
            literal(None),
            literal("snapshot_file"),
            P.label,
            S.memo,
        )
        .join(P, S.snapshot_id == P.id)
        .join(F, S.asset_id == F.id)
        .where(F.state == "active")
    )
    return union_all(current, versions, snapshots).subquery()


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
        rows_source = document_search_rows()
        c = rows_source.c
        statement = select(
            *[column for column in c if column.key != "memo"],
            func.substr(c.memo, 1, 200).label("memo"),
            T.title,
            T.status,
            T.deleted_at,
        ).join(T, c.task_id == T.id)
        if not query.include_trash:
            statement = statement.where(T.deleted_at.is_(None))
        if not query.include_detached:
            statement = statement.where(c.detached_at.is_(None))
        if query.source_kind != "all":
            statement = statement.where(c.source_kind == query.source_kind)
        if query.search.strip():
            # autoescape keeps SQL wildcard characters literal; values are bound.
            statement = statement.where(
                and_(
                    *(
                        or_(
                            *(
                                column.contains(term, autoescape=True)
                                for column in (c.name, c.original_name, c.label, c.memo)
                            )
                        )
                        for term in query.search.strip().split()
                    )
                )
            )
        if query.attached_after is not None:
            statement = statement.where(c.created_at >= query.attached_after)
        if query.attached_before is not None:
            statement = statement.where(c.created_at < query.attached_before)
        if query.kind is not AttachmentKind.ALL:
            suffixes = (
                tuple(suffix for group in EXTENSIONS.values() for suffix in group)
                if query.kind is AttachmentKind.OTHER
                else EXTENSIONS[query.kind]
            )
            matches = or_(
                *(func.substr(func.lower(c.name), -len(suffix)) == suffix for suffix in suffixes)
            )
            statement = statement.where(~matches if query.kind is AttachmentKind.OTHER else matches)
        if query.cursor is not None:
            cursor = query.cursor
            statement = statement.where(
                or_(
                    c.created_at < cursor.created_at,
                    and_(c.created_at == cursor.created_at, c.source_kind < cursor.source_kind),
                    and_(
                        c.created_at == cursor.created_at,
                        c.source_kind == cursor.source_kind,
                        c.id < cursor.attachment_id,
                    ),
                )
            )
        statement = statement.order_by(
            c.created_at.desc(), c.source_kind.desc(), c.id.desc()
        ).limit(query.limit + 1)
        with self._sessions.transaction() as session:
            connection = cast(sqlite3.Connection, session.connection().connection.driver_connection)
            connection.set_progress_handler(lambda: int(interrupted()), 1000)
            try:
                rows = session.execute(statement).all()
                if interrupted():
                    raise AttachmentSearchInterrupted("검색 조건을 더 구체적으로 입력해 주세요.")
            except OperationalError as error:
                if interrupted():
                    raise AttachmentSearchInterrupted(
                        "검색 조건을 더 구체적으로 입력해 주세요."
                    ) from error
                raise
            finally:
                connection.set_progress_handler(None, 0)
        items = tuple(
            AttachmentSearchHit(
                attachment_id=row.id,
                task_id=row.task_id,
                original_name=row.name,
                size_bytes=row.size_bytes or 0,
                created_at=row.created_at,
                task_title=row.title,
                task_status=TaskStatus(row.status),
                deleted=row.deleted_at is not None,
                detached=row.detached_at is not None,
                missing=row.missing_at is not None,
                size_observed=row.observed_at is not None,
                source_kind=row.source_kind,
                label=row.label,
                memo=row.memo[:200],
            )
            for row in rows[: query.limit]
        )
        next_cursor = (
            AttachmentCursor(items[-1].created_at, items[-1].attachment_id, items[-1].source_kind)
            if items
            else None
        )
        return AttachmentSearchPage(items, next_cursor, len(rows) > query.limit)
