from __future__ import annotations

from typing import Any

from sqlalchemy import and_, false, func, literal_column, select, text
from sqlalchemy.orm import Session

from officeflow.infrastructure.database.models import AttachmentRecord


def fts_prefix_query(value: str) -> str:
    """Build a safe FTS5 AND query with prefix matching for each user term."""
    terms = value.strip().split()
    return " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)


def attachment_name_predicate(value: str) -> Any:
    """Literal substring AND matching, with trigram candidate acceleration."""
    if len(value.strip()) < 2:
        return false()
    terms = value.strip().split()
    name = func.lower(AttachmentRecord.original_name)
    conditions = [
        func.instr(
            name,
            term.translate(
                str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
            ),
        )
        > 0
        for term in terms
    ]
    long_terms = [term for term in terms if len(term) >= 3]
    if long_terms:
        expression = " AND ".join('"' + term.replace('"', '""') + '"' for term in long_terms)
        candidates: Any = (
            select(literal_column("rowid"))
            .select_from(text("attachment_search"))
            .where(
                text("attachment_search MATCH :attachment_fts").bindparams(
                    attachment_fts=expression
                )
            )
        )
        conditions.append(AttachmentRecord.id.in_(candidates))
    return and_(*conditions)


def matching_attachment_tasks(value: str) -> Any:
    return select(AttachmentRecord.task_id).where(
        AttachmentRecord.detached_at.is_(None), attachment_name_predicate(value)
    )


def attachment_match_summaries(
    session: Session,
    task_ids: tuple[int, ...],
    value: str,
) -> dict[int, tuple[int, str, int]]:
    if not task_ids or len(value.strip()) < 2:
        return {}
    rows = (
        select(
            AttachmentRecord.task_id.label("task_id"),
            AttachmentRecord.id.label("id"),
            AttachmentRecord.original_name.label("name"),
            func.count().over(partition_by=AttachmentRecord.task_id).label("total"),
            func.row_number()
            .over(
                partition_by=AttachmentRecord.task_id,
                order_by=(AttachmentRecord.created_at.desc(), AttachmentRecord.id.desc()),
            )
            .label("position"),
        )
        .where(
            AttachmentRecord.task_id.in_(task_ids),
            AttachmentRecord.detached_at.is_(None),
            attachment_name_predicate(value),
        )
        .subquery()
    )
    return {
        int(row.task_id): (int(row.id), str(row.name), int(row.total))
        for row in session.execute(select(rows).where(rows.c.position == 1))
    }
