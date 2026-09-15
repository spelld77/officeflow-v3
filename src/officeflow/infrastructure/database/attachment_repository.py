from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from officeflow.domain.attachment import Attachment
from officeflow.infrastructure.database.models import AttachmentRecord, TaskRecord
from officeflow.infrastructure.database.session import SessionFactory


class SqlAlchemyAttachmentRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def list_attachments(self, task_id: int) -> tuple[Attachment, ...]:
        statement = (
            select(AttachmentRecord)
            .where(AttachmentRecord.task_id == task_id)
            .order_by(AttachmentRecord.created_at.desc(), AttachmentRecord.id.desc())
        )
        with self._sessions.transaction() as session:
            return tuple(self._to_domain(record) for record in session.scalars(statement).all())

    def get_attachment(self, attachment_id: int) -> Attachment | None:
        with self._sessions.transaction() as session:
            record = session.get(AttachmentRecord, attachment_id)
            return self._to_domain(record) if record is not None else None

    def add_attachment(self, attachment: Attachment) -> Attachment:
        with self._sessions.transaction() as session:
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
            )
            session.add(record)
            session.flush()
            attachment_id = record.id
        return self._get_required(attachment_id)

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
        )
