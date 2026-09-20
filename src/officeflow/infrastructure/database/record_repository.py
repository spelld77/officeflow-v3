from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, literal_column, or_, select, text

from officeflow.application.records import WorkLogPage
from officeflow.domain.enums import TaskPriority
from officeflow.domain.records import ChecklistItem, WorkLog
from officeflow.infrastructure.database.models import (
    ChecklistItemRecord,
    TaskRecord,
    WorkLogRecord,
)
from officeflow.infrastructure.database.search import fts_prefix_query
from officeflow.infrastructure.database.session import SessionFactory


class SqlAlchemyRecordRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def list_checklist(self, task_id: int) -> tuple[ChecklistItem, ...]:
        statement = (
            select(ChecklistItemRecord)
            .where(ChecklistItemRecord.task_id == task_id)
            .order_by(ChecklistItemRecord.position, ChecklistItemRecord.id)
        )
        with self._sessions.transaction() as session:
            return tuple(self._to_checklist(record) for record in session.scalars(statement).all())

    def add_checklist_item(self, item: ChecklistItem) -> ChecklistItem:
        with self._sessions.transaction() as session:
            if session.get(TaskRecord, item.task_id) is None:
                raise LookupError(f"업무 {item.task_id}을(를) 찾을 수 없습니다.")
            record = ChecklistItemRecord(
                task_id=item.task_id,
                content=item.content,
                is_done=item.is_done,
                position=item.position,
                completed_at=item.completed_at,
            )
            session.add(record)
            session.flush()
            item_id = record.id
        return self._get_checklist_required(item_id)

    def update_checklist_item(self, item: ChecklistItem) -> ChecklistItem:
        if item.id is None:
            raise ValueError("저장되지 않은 체크리스트 항목은 수정할 수 없습니다.")
        with self._sessions.transaction() as session:
            record = session.get(ChecklistItemRecord, item.id)
            if record is None:
                raise LookupError(f"체크리스트 {item.id}을(를) 찾을 수 없습니다.")
            record.content = item.content
            record.is_done = item.is_done
            record.position = item.position
            record.completed_at = item.completed_at
        return self._get_checklist_required(item.id)

    def delete_checklist_item(self, item_id: int) -> None:
        with self._sessions.transaction() as session:
            record = session.get(ChecklistItemRecord, item_id)
            if record is None:
                raise LookupError(f"체크리스트 {item_id}을(를) 찾을 수 없습니다.")
            task_id = record.task_id
            session.delete(record)
            session.flush()
            remaining = list(
                session.scalars(
                    select(ChecklistItemRecord)
                    .where(ChecklistItemRecord.task_id == task_id)
                    .order_by(ChecklistItemRecord.position, ChecklistItemRecord.id)
                ).all()
            )
            for position, remaining_record in enumerate(remaining):
                remaining_record.position = position

    def reorder_checklist(self, task_id: int, item_ids: tuple[int, ...]) -> None:
        with self._sessions.transaction() as session:
            records = list(
                session.scalars(
                    select(ChecklistItemRecord)
                    .where(ChecklistItemRecord.task_id == task_id)
                    .order_by(ChecklistItemRecord.position, ChecklistItemRecord.id)
                ).all()
            )
            existing_ids = {record.id for record in records}
            if len(item_ids) != len(set(item_ids)) or set(item_ids) != existing_ids:
                raise ValueError("체크리스트 전체 항목을 중복 없이 지정해야 합니다.")
            records_by_id = {record.id: record for record in records}
            for position, item_id in enumerate(item_ids):
                records_by_id[item_id].position = position

    def get_work_log(self, log_id: int) -> WorkLog | None:
        with self._sessions.transaction() as session:
            record = session.get(WorkLogRecord, log_id)
            return self._to_work_log(record) if record is not None else None

    def list_work_logs(
        self,
        *,
        log_date: date | None = None,
        task_id: int | None = None,
        search: str = "",
        occurrence_id: int | None = None,
        occurrence_only: bool = False,
        limit: int | None = None,
    ) -> tuple[WorkLog, ...]:
        statement = select(WorkLogRecord)
        if log_date is not None:
            statement = statement.where(WorkLogRecord.log_date == log_date)
        if task_id is not None:
            statement = statement.where(WorkLogRecord.task_id == task_id)
        if occurrence_only:
            statement = statement.where(WorkLogRecord.occurrence_id == occurrence_id)
        normalized = search.strip()
        if normalized:
            statement = statement.where(self._work_log_search_predicate(normalized))
        statement = statement.order_by(
            WorkLogRecord.log_date.desc(),
            WorkLogRecord.updated_at.desc(),
            WorkLogRecord.id.desc(),
        )
        if limit is not None:
            statement = statement.limit(limit)
        with self._sessions.transaction() as session:
            return tuple(self._to_work_log(record) for record in session.scalars(statement).all())

    def query_work_logs(
        self,
        *,
        search: str = "",
        date_from: date | None = None,
        date_to: date | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> WorkLogPage:
        predicates: list[Any] = []
        if date_from is not None:
            predicates.append(WorkLogRecord.log_date >= date_from)
        if date_to is not None:
            predicates.append(WorkLogRecord.log_date <= date_to)
        normalized = search.strip()
        if normalized:
            predicates.append(self._work_log_search_predicate(normalized))
        count_statement = select(func.count(WorkLogRecord.id)).where(*predicates)
        statement = (
            select(WorkLogRecord)
            .where(*predicates)
            .order_by(
                WorkLogRecord.log_date.desc(),
                WorkLogRecord.updated_at.desc(),
                WorkLogRecord.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        with self._sessions.transaction() as session:
            total = int(session.scalar(count_statement) or 0)
            items = tuple(
                self._to_work_log(record) for record in session.scalars(statement).all()
            )
        return WorkLogPage(items=items, total=total, offset=offset, limit=limit)

    def has_duplicate_work_log(
        self,
        *,
        task_id: int | None,
        log_date: date,
        content: str,
        exclude_log_id: int | None = None,
    ) -> bool:
        statement = select(WorkLogRecord.id).where(
            WorkLogRecord.log_date == log_date,
            WorkLogRecord.content == content,
            (
                WorkLogRecord.task_id.is_(None)
                if task_id is None
                else WorkLogRecord.task_id == task_id
            ),
        )
        if exclude_log_id is not None:
            statement = statement.where(WorkLogRecord.id != exclude_log_id)
        with self._sessions.transaction() as session:
            return session.scalar(statement.limit(1)) is not None

    def count_work_logs(self, task_ids: tuple[int, ...]) -> dict[int, int]:
        if not task_ids:
            return {}
        statement = (
            select(WorkLogRecord.task_id, func.count(WorkLogRecord.id))
            .where(WorkLogRecord.task_id.in_(task_ids))
            .group_by(WorkLogRecord.task_id)
        )
        with self._sessions.transaction() as session:
            return {
                int(task_id): int(count)
                for task_id, count in session.execute(statement)
                if task_id is not None
            }

    @staticmethod
    def _work_log_search_predicate(search: str) -> Any:
        query = fts_prefix_query(search)
        matching_logs: Any = (
            select(literal_column("rowid"))
            .select_from(text("work_log_search"))
            .where(
                text("work_log_search MATCH :work_log_fts").bindparams(
                    work_log_fts=query
                )
            )
        )
        matching_tasks: Any = (
            select(literal_column("rowid"))
            .select_from(text("task_search"))
            .where(text("task_search MATCH :task_fts").bindparams(task_fts=query))
        )
        return or_(
            WorkLogRecord.id.in_(matching_logs),
            WorkLogRecord.task_id.in_(matching_tasks),
        )

    def add_work_log(self, work_log: WorkLog) -> WorkLog:
        with self._sessions.transaction() as session:
            if work_log.task_id is not None and session.get(TaskRecord, work_log.task_id) is None:
                raise LookupError(f"업무 {work_log.task_id}을(를) 찾을 수 없습니다.")
            record = WorkLogRecord()
            self._copy_work_log(work_log, record)
            session.add(record)
            session.flush()
            log_id = record.id
        return self._get_work_log_required(log_id)

    def update_work_log(self, work_log: WorkLog) -> WorkLog:
        if work_log.id is None:
            raise ValueError("저장되지 않은 업무일지는 수정할 수 없습니다.")
        with self._sessions.transaction() as session:
            record = session.get(WorkLogRecord, work_log.id)
            if record is None:
                raise LookupError(f"업무일지 {work_log.id}을(를) 찾을 수 없습니다.")
            self._copy_work_log(work_log, record)
        return self._get_work_log_required(work_log.id)

    def delete_work_log(self, log_id: int) -> None:
        with self._sessions.transaction() as session:
            record = session.get(WorkLogRecord, log_id)
            if record is None:
                raise LookupError(f"업무일지 {log_id}을(를) 찾을 수 없습니다.")
            session.delete(record)

    def _get_checklist_required(self, item_id: int) -> ChecklistItem:
        with self._sessions.transaction() as session:
            record = session.get(ChecklistItemRecord, item_id)
            if record is None:
                raise LookupError(f"체크리스트 {item_id}을(를) 찾을 수 없습니다.")
            return self._to_checklist(record)

    def _get_work_log_required(self, log_id: int) -> WorkLog:
        work_log = self.get_work_log(log_id)
        if work_log is None:
            raise LookupError(f"업무일지 {log_id}을(를) 찾을 수 없습니다.")
        return work_log

    @staticmethod
    def _to_checklist(record: ChecklistItemRecord) -> ChecklistItem:
        return ChecklistItem(
            id=record.id,
            task_id=record.task_id,
            content=record.content,
            is_done=record.is_done,
            position=record.position,
            completed_at=record.completed_at,
        )

    @staticmethod
    def _to_work_log(record: WorkLogRecord) -> WorkLog:
        return WorkLog(
            id=record.id,
            task_id=record.task_id,
            occurrence_id=record.occurrence_id,
            log_date=record.log_date,
            content=record.content,
            result=record.result,
            priority_snapshot=TaskPriority(record.priority_snapshot),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _copy_work_log(work_log: WorkLog, record: WorkLogRecord) -> None:
        record.task_id = work_log.task_id
        record.occurrence_id = work_log.occurrence_id
        record.log_date = work_log.log_date
        record.content = work_log.content
        record.result = work_log.result
        record.priority_snapshot = work_log.priority_snapshot.value
        record.created_at = work_log.created_at
        record.updated_at = work_log.updated_at
