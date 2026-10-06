from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from officeflow.domain.attachment import Attachment, AttachmentMove
from officeflow.infrastructure.database.models import AttachmentRecord, TaskRecord
from officeflow.infrastructure.database.search import attachment_match_summaries
from officeflow.infrastructure.database.session import SessionFactory


class SqlAlchemyAttachmentRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def list_attachments(self, task_id: int) -> tuple[Attachment, ...]:
        statement = (
            select(AttachmentRecord)
            .where(
                AttachmentRecord.task_id == task_id,
                AttachmentRecord.detached_at.is_(None),
            )
            .order_by(AttachmentRecord.sort_order, AttachmentRecord.created_at.desc(), AttachmentRecord.id.desc())
        )
        with self._sessions.transaction() as session:
            return tuple(self._to_domain(record) for record in session.scalars(statement).all())

    def filename_matches_for_tasks(
        self, task_ids: tuple[int, ...], search: str
    ) -> dict[int, tuple[int, str, int]]:
        with self._sessions.transaction() as session:
            return attachment_match_summaries(session, task_ids, search)

    def get_attachment(self, attachment_id: int) -> Attachment | None:
        with self._sessions.transaction() as session:
            record = session.get(AttachmentRecord, attachment_id)
            return self._to_domain(record) if record is not None else None

    def list_detached(self) -> tuple[Attachment, ...]:
        statement = (
            select(AttachmentRecord)
            .where(AttachmentRecord.detached_at.is_not(None))
            .order_by(AttachmentRecord.detached_at.desc(), AttachmentRecord.id.desc())
        )
        with self._sessions.transaction() as session:
            return tuple(self._to_domain(record) for record in session.scalars(statement).all())

    def find_active_by_checksum(self, task_id: int, checksum: str) -> Attachment | None:
        statement = select(AttachmentRecord).where(
            AttachmentRecord.task_id == task_id,
            AttachmentRecord.checksum == checksum,
            AttachmentRecord.detached_at.is_(None),
        )
        with self._sessions.transaction() as session:
            record = session.scalars(statement).first()
            return self._to_domain(record) if record is not None else None

    def add_attachment(self, attachment: Attachment) -> Attachment:
        with self._sessions.transaction() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            task = session.get(TaskRecord, attachment.task_id)
            if task is None or task.deleted_at is not None:
                raise LookupError(f"업무 {attachment.task_id}을(를) 찾을 수 없습니다.")
            record = AttachmentRecord(
                task_id=attachment.task_id,
                original_name=attachment.original_name,
                stored_name=attachment.stored_name,
                relative_path=attachment.relative_path,
                size_bytes=attachment.size_bytes,
                checksum=attachment.checksum,
                created_at=attachment.created_at,
                missing_at=attachment.missing_at,
                detached_at=attachment.detached_at,
                sort_order=(session.scalar(select(func.max(AttachmentRecord.sort_order)).where(
                    AttachmentRecord.task_id == attachment.task_id
                )) or 0) + 10,
            )
            session.add(record)
            session.flush()
            attachment_id = record.id
        return self._get_required(attachment_id)

    def move_attachment(self, task_id: int, attachment_id: int, movement: AttachmentMove) -> tuple[Attachment, ...]:
        with self._sessions.transaction() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            task = session.get(TaskRecord, task_id)
            if task is None or task.deleted_at is not None:
                raise LookupError("휴지통 또는 없는 업무의 첨부 순서는 변경할 수 없습니다.")
            records = list(session.scalars(select(AttachmentRecord).where(
                AttachmentRecord.task_id == task_id, AttachmentRecord.detached_at.is_(None)
            ).order_by(AttachmentRecord.sort_order, AttachmentRecord.created_at.desc(), AttachmentRecord.id.desc())))
            row = next((i for i, item in enumerate(records) if item.id == attachment_id), None)
            if row is None:
                raise LookupError("이 업무에 연결된 첨부파일을 찾을 수 없습니다.")
            slots = [item.sort_order for item in records]
            if any(slot <= 0 for slot in slots) or len(set(slots)) != len(slots):
                raise ValueError("첨부 순서 정보가 올바르지 않습니다. 데이터 상태를 확인하세요.")
            target = {
                AttachmentMove.UP: max(0, row - 1),
                AttachmentMove.DOWN: min(len(records) - 1, row + 1),
                AttachmentMove.FIRST: 0,
                AttachmentMove.LAST: len(records) - 1,
            }[movement]
            records.insert(target, records.pop(row))
            for item, slot in zip(records, slots, strict=True):
                item.sort_order = slot
            session.flush()
            return tuple(self._to_domain(item) for item in records)

    def set_missing_at(
        self,
        attachment_id: int,
        missing_at: datetime | None,
    ) -> Attachment:
        with self._sessions.transaction() as session:
            record = session.get(AttachmentRecord, attachment_id)
            if record is None:
                raise LookupError(f"첨부파일 {attachment_id}을(를) 찾을 수 없습니다.")
            record.missing_at = missing_at
        return self._get_required(attachment_id)

    def set_checksum(self, attachment_id: int, checksum: str) -> Attachment:
        with self._sessions.transaction() as session:
            record = session.get(AttachmentRecord, attachment_id)
            if record is None:
                raise LookupError(f"첨부파일 {attachment_id}을(를) 찾을 수 없습니다.")
            record.checksum = checksum
        return self._get_required(attachment_id)

    def set_detached_at(
        self,
        attachment_id: int,
        detached_at: datetime | None,
    ) -> Attachment:
        with self._sessions.transaction() as session:
            record = session.get(AttachmentRecord, attachment_id)
            if record is None:
                raise LookupError(f"첨부파일 {attachment_id}을(를) 찾을 수 없습니다.")
            record.detached_at = detached_at
        return self._get_required(attachment_id)

    def delete_attachment(self, attachment_id: int) -> None:
        with self._sessions.transaction() as session:
            record = session.get(AttachmentRecord, attachment_id)
            if record is None:
                raise LookupError(f"첨부파일 {attachment_id}을(를) 찾을 수 없습니다.")
            session.delete(record)

    def _get_required(self, attachment_id: int) -> Attachment:
        attachment = self.get_attachment(attachment_id)
        if attachment is None:
            raise LookupError(f"첨부파일 {attachment_id}을(를) 찾을 수 없습니다.")
        return attachment

    @staticmethod
    def _to_domain(record: AttachmentRecord) -> Attachment:
        return Attachment(
            id=record.id,
            task_id=record.task_id,
            original_name=record.original_name,
            stored_name=record.stored_name,
            relative_path=record.relative_path,
            size_bytes=record.size_bytes,
            checksum=record.checksum,
            created_at=record.created_at,
            missing_at=record.missing_at,
            detached_at=record.detached_at,
            sort_order=record.sort_order,
        )
