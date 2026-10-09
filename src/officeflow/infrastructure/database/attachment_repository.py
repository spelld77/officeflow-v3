from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update

from officeflow.domain.attachment import (
    Attachment,
    AttachmentCheckStatus,
    AttachmentMove,
    AttachmentRole,
    FileObservation,
)
from officeflow.infrastructure.database.file_references import (
    assert_supported_asset_references,
    asset_paths_for_task,
    asset_reference_count,
)
from officeflow.infrastructure.database.models import (
    AttachmentRecord,
    FileAssetRecord,
    TaskRecord,
)
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
            .order_by(
                AttachmentRecord.sort_order,
                AttachmentRecord.created_at.desc(),
                AttachmentRecord.id.desc(),
            )
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
                observed_size_bytes=attachment.observed_size_bytes,
                observed_checksum=attachment.observed_checksum,
                observed_mtime_ns=attachment.observed_mtime_ns,
                observed_at=attachment.observed_at,
                last_check_status=attachment.last_check_status,
                last_checked_at=attachment.last_checked_at,
                created_at=attachment.created_at,
                missing_at=attachment.missing_at,
                detached_at=attachment.detached_at,
                sort_order=(
                    session.scalar(
                        select(func.max(AttachmentRecord.sort_order)).where(
                            AttachmentRecord.task_id == attachment.task_id
                        )
                    )
                    or 0
                )
                + 10,
            )
            session.add(record)
            session.flush()
            attachment_id = record.id
        return self._get_required(attachment_id)

    def move_attachment(
        self, task_id: int, attachment_id: int, movement: AttachmentMove
    ) -> tuple[Attachment, ...]:
        with self._sessions.transaction() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            task = session.get(TaskRecord, task_id)
            if task is None or task.deleted_at is not None:
                raise LookupError("휴지통 또는 없는 업무의 첨부 순서는 변경할 수 없습니다.")
            records = list(
                session.scalars(
                    select(AttachmentRecord)
                    .where(
                        AttachmentRecord.task_id == task_id, AttachmentRecord.detached_at.is_(None)
                    )
                    .order_by(
                        AttachmentRecord.sort_order,
                        AttachmentRecord.created_at.desc(),
                        AttachmentRecord.id.desc(),
                    )
                )
            )
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
                if item.sort_order != slot:
                    item.metadata_revision += 1
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
            record.asset.missing_at = missing_at
        return self._get_required(attachment_id)

    def set_checksum(self, attachment_id: int, checksum: str) -> Attachment:
        with self._sessions.transaction() as session:
            record = session.get(AttachmentRecord, attachment_id)
            if record is None:
                raise LookupError(f"첨부파일 {attachment_id}을(를) 찾을 수 없습니다.")
            record.checksum = checksum
        return self._get_required(attachment_id)

    def update_observation_if_current(
        self,
        expected: Attachment,
        observation: FileObservation | None,
        status: AttachmentCheckStatus,
        checked_at: datetime,
    ) -> Attachment | None:
        values: dict[str, object] = {
            "last_check_status": status.value,
            "last_checked_at": checked_at,
        }
        if observation:
            values.update(
                observed_size_bytes=observation.size_bytes,
                observed_checksum=observation.checksum,
                observed_mtime_ns=observation.mtime_ns,
                observed_at=observation.observed_at,
                missing_at=None,
            )
        elif status is AttachmentCheckStatus.MISSING:
            values["missing_at"] = expected.missing_at or checked_at
        statement = (
            update(FileAssetRecord)
            .where(
                FileAssetRecord.id.in_(
                    select(AttachmentRecord.asset_id).where(
                        AttachmentRecord.id == expected.id_required,
                        AttachmentRecord.asset_id == expected.asset_id,
                        AttachmentRecord.detached_at == expected.detached_at,
                    )
                ),
                FileAssetRecord.relative_path == expected.relative_path,
                FileAssetRecord.observed_at == expected.observed_at,
                FileAssetRecord.last_checked_at == expected.last_checked_at,
            )
            .values(**values)
        )
        with self._sessions.transaction() as session:
            changed = session.execute(statement).rowcount  # type: ignore[attr-defined]
        return self.get_attachment(expected.id_required) if changed else None

    def is_path_registered(self, relative_path: str) -> bool:
        with self._sessions.transaction() as session:
            return (
                session.scalar(
                    select(FileAssetRecord.id)
                    .where(FileAssetRecord.relative_path == relative_path)
                    .limit(1)
                )
                is not None
            )

    def paths_for_task(self, task_id: int) -> tuple[str, ...]:
        with self._sessions.transaction() as session:
            return asset_paths_for_task(session, task_id)

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
            record.metadata_revision += 1
        return self._get_required(attachment_id)

    def delete_attachment(self, attachment_id: int) -> None:
        self._delete_attachment(attachment_id, require_unreferenced=False)

    def delete_unreferenced_attachment(self, attachment_id: int) -> None:
        self._delete_attachment(attachment_id, require_unreferenced=True)

    def _delete_attachment(self, attachment_id: int, *, require_unreferenced: bool) -> None:
        with self._sessions.transaction() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            assert_supported_asset_references(session)
            record = session.get(AttachmentRecord, attachment_id)
            if record is None:
                raise LookupError(f"첨부파일 {attachment_id}을(를) 찾을 수 없습니다.")
            asset = record.asset
            if require_unreferenced and asset_reference_count(
                session, asset.id, exclude_attachment=attachment_id
            ):
                raise ValueError(
                    "파일 보존 참조가 변경되었습니다. 파일을 복구하고 다시 확인하세요."
                )
            session.delete(record)
            session.flush()
            if not asset_reference_count(session, asset.id):
                session.delete(asset)

    def has_other_asset_references(self, attachment_id: int) -> bool:
        with self._sessions.transaction() as session:
            assert_supported_asset_references(session)
            record = session.get(AttachmentRecord, attachment_id)
            if record is None:
                raise LookupError("첨부파일을 찾을 수 없습니다.")
            return bool(
                asset_reference_count(session, record.asset_id, exclude_attachment=attachment_id)
            )

    def update_metadata(
        self,
        attachment_id: int,
        *,
        expected_revision: int,
        display_name: str | None = None,
        memo: str | None = None,
    ) -> Attachment:
        with self._sessions.transaction() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            record = session.get(AttachmentRecord, attachment_id)
            if record is None:
                raise LookupError("첨부파일을 찾을 수 없습니다.")
            task = session.get(TaskRecord, record.task_id)
            if task is None or task.deleted_at is not None or record.detached_at is not None:
                raise ValueError("업무/첨부를 복원한 뒤 정보를 변경하세요.")
            if record.metadata_revision != expected_revision:
                raise ValueError("첨부 정보가 변경되었습니다. 새로고침 후 다시 시도하세요.")
            if display_name is not None:
                record.display_name = display_name
            if memo is not None:
                record.memo = memo
            record.metadata_revision += 1
        return self._get_required(attachment_id)

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
            observed_size_bytes=record.observed_size_bytes,
            observed_checksum=record.observed_checksum,
            observed_mtime_ns=record.observed_mtime_ns,
            observed_at=record.observed_at,
            last_check_status=AttachmentCheckStatus(record.last_check_status)
            if record.last_check_status
            else None,
            last_checked_at=record.last_checked_at,
            display_name=record.display_name,
            asset_id=record.asset_id,
            family_id=record.family_id,
            role=AttachmentRole(record.role),
            memo=record.memo,
            metadata_revision=record.metadata_revision,
            fixed_size_bytes=record.asset.fixed_size_bytes,
            fixed_checksum=record.asset.fixed_checksum,
            source_attachment_id=record.source_attachment_id,
            source_version_id=record.source_version_id,
            source_payload=record.source_payload,
        )
