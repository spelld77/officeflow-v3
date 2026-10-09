"""One transaction for file assets, document relationships, and completion receipt."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from officeflow.application.attachment_documents import (
    CommittedDocument,
    DocumentCommand,
    DocumentOperation,
    DocumentResult,
    DocumentSource,
    InternalCapture,
    fingerprint,
)
from officeflow.infrastructure.database.file_references import assert_supported_asset_references
from officeflow.infrastructure.database.models import (
    AttachmentRecord,
    AttachmentVersionRecord,
    DocumentFamilyRecord,
    FileAssetRecord,
    FileOperationReceiptRecord,
    TaskRecord,
)
from officeflow.infrastructure.database.session import SessionFactory


class SqlAlchemyDocumentRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def source(self, command: DocumentCommand) -> DocumentSource:
        with self._sessions.transaction() as session:
            return self._source(session, command)

    @staticmethod
    def _source(session: Session, command: DocumentCommand) -> DocumentSource:
        if command.source_kind == "cache":
            from officeflow.infrastructure.attachments.view_cache import decode_cache_source

            info = decode_cache_source(command.cache_payload)
            family = session.get(DocumentFamilyRecord, info["family_id"])
            task = session.get(TaskRecord, info["task_id"])
            if (
                family is None
                or task is None
                or family.task_id != task.id
                or task.deleted_at is not None
            ):
                raise ValueError("업무/문서 계열을 복원한 뒤 사본을 보존하세요.")
            return DocumentSource(
                "cache",
                command.source_id,
                task.id,
                family.id,
                info["asset_id"],
                info["relative_path"],
                info["name"],
                "work",
                command.expected_revision,
                fingerprint(info),
                command.cache_payload,
                info["size"],
                info["checksum"],
            )
        record: AttachmentRecord | AttachmentVersionRecord | None
        record = (
            session.get(AttachmentRecord, command.source_id)
            if command.source_kind == "attachment"
            else session.get(AttachmentVersionRecord, command.source_id)
        )
        if record is None:
            raise LookupError("출처 첨부/보관본을 찾을 수 없습니다.")
        task = session.get(TaskRecord, record.task_id)
        if task is None or task.deleted_at is not None:
            raise ValueError("업무를 복원한 뒤 파일을 생성하세요.")
        if isinstance(record, AttachmentRecord) and record.detached_at is not None:
            raise ValueError("해제 첨부를 복원한 뒤 파일을 생성하세요.")
        if record.metadata_revision != command.expected_revision or record.asset.state != "active":
            raise ValueError("첨부 정보가 변경되었습니다. 새로고침 후 다시 시도하세요.")
        frozen = isinstance(record, AttachmentVersionRecord) or record.role == "original"
        if record.asset.kind != ("frozen" if frozen else "mutable") or (
            frozen
            and (record.asset.fixed_size_bytes is None or record.asset.fixed_checksum is None)
        ):
            raise ValueError("역할과 자산의 고정 기준이 일치하지 않습니다. 자료를 보존했습니다.")
        name = record.display_name if isinstance(record, AttachmentRecord) else record.original_name
        role = record.role if isinstance(record, AttachmentRecord) else "version"
        if command.operation is DocumentOperation.RELEASE_ORIGINAL and role != "original":
            raise ValueError("현재 원본 첨부만 지정 해제할 수 있습니다.")
        state = {
            "kind": command.source_kind,
            "id": record.id,
            "task_id": record.task_id,
            "task_deleted_at": None,
            "family_id": record.family_id,
            "asset_id": record.asset_id,
            "relative_path": record.asset.relative_path,
            "asset_kind": record.asset.kind,
            "asset_state": record.asset.state,
            "fixed_size": record.asset.fixed_size_bytes,
            "fixed_checksum": record.asset.fixed_checksum,
            "name": name,
            "role": role,
            "revision": record.metadata_revision,
            "memo": record.memo,
            "order": record.sort_order if isinstance(record, AttachmentRecord) else None,
            "label": record.label if isinstance(record, AttachmentVersionRecord) else None,
            "source_attachment_id": record.source_attachment_id,
            "source_version_id": record.source_version_id,
            "source_payload": record.source_payload,
        }
        provenance = {
            "version": 1,
            "kind": command.source_kind,
            "id": record.id,
            "asset_id": record.asset_id,
            "family_id": record.family_id,
            "name": name,
            "role": role,
            "created_at": record.created_at.isoformat(),
        }
        return DocumentSource(
            command.source_kind,
            record.id,
            record.task_id,
            record.family_id,
            record.asset_id,
            record.asset.relative_path,
            name,
            role,
            record.metadata_revision,
            fingerprint(state),
            json.dumps(provenance, ensure_ascii=False, sort_keys=True),
            record.asset.fixed_size_bytes,
            record.asset.fixed_checksum,
        )

    def committed(self, command: DocumentCommand) -> CommittedDocument | None:
        with self._sessions.transaction() as session:
            return self._committed(session, command)

    @staticmethod
    def _committed(session: Session, command: DocumentCommand) -> CommittedDocument | None:
        receipt = session.get(FileOperationReceiptRecord, command.job_id)
        if receipt is None:
            return None
        payload = json.loads(receipt.payload)
        if (
            receipt.payload_version != 1
            or receipt.operation_kind != command.operation.value
            or payload.get("request_fingerprint") != command.request_fingerprint
        ):
            raise ValueError("같은 작업 ID에 다른 요청을 사용할 수 없습니다.")
        result = DocumentResult(**payload["result"])
        if (
            result.job_id != command.job_id
            or type(payload.get("size")) is not int
            or payload["size"] < 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("checksum", "")))
            or payload.get("frozen") is not command.operation.creates_frozen
            or (command.operation is DocumentOperation.VERSION) != (result.version_id is not None)
            or (command.operation is DocumentOperation.ORIGINAL)
            != (result.retained_attachment_id is not None)
            or (command.operation is DocumentOperation.RELEASE_ORIGINAL)
            != (result.retained_version_id is not None)
        ):
            raise ValueError("작업 영수증의 파일 검증/결과 정보가 올바르지 않습니다.")
        asset = session.get(FileAssetRecord, result.asset_id)
        relative: str | None = None
        if asset is not None and asset.state == "active":
            if asset.kind != ("frozen" if payload["frozen"] else "mutable") or (
                payload["frozen"]
                and (
                    asset.fixed_size_bytes != payload["size"]
                    or asset.fixed_checksum != payload["checksum"]
                )
            ):
                raise ValueError("완료 영수증과 자산의 고정 기준이 일치하지 않습니다.")
            item: AttachmentRecord | AttachmentVersionRecord | None
            if result.attachment_id is not None:
                item = session.get(AttachmentRecord, result.attachment_id)
            else:
                item = session.get(AttachmentVersionRecord, result.version_id)
            if item is not None and item.asset_id == asset.id:
                relative = asset.relative_path
        return CommittedDocument(
            result, relative, payload["frozen"], payload["size"], payload["checksum"]
        )

    def path_registered(self, relative_path: str) -> bool:
        with self._sessions.transaction() as session:
            return (
                session.scalar(
                    select(FileAssetRecord.id)
                    .where(FileAssetRecord.relative_path == relative_path)
                    .limit(1)
                )
                is not None
            )

    def finalize(
        self, command: DocumentCommand, expected: DocumentSource, capture: InternalCapture
    ) -> DocumentResult:
        with self._sessions.transaction() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            assert_supported_asset_references(session)
            if self._committed(session, command) is not None:
                # Do not adopt/delete an extra published file from a racing caller.
                raise ValueError(
                    "작업이 이미 확정되었습니다. 추가 준비 파일은 점검 대상으로 보존합니다."
                )
            if self._source(session, command) != expected or capture.job_id != command.job_id:
                raise ValueError("출처 정보가 변경되어 파일을 확정하지 않았습니다.")
            now = datetime.now(UTC)
            frozen = command.operation.creates_frozen
            asset = FileAssetRecord(
                id=capture.asset_id,
                kind="frozen" if frozen else "mutable",
                state="active",
                stored_name=capture.stored_name,
                relative_path=capture.relative_path,
                created_at=capture.captured_at,
                fixed_size_bytes=capture.size if frozen else None,
                fixed_checksum=capture.checksum if frozen else None,
                frozen_at=now if frozen else None,
            )
            session.add(asset)
            session.flush()
            source_links = {
                "source_attachment_id": expected.id if expected.kind == "attachment" else None,
                "source_version_id": expected.id if expected.kind == "version" else None,
                "source_payload": expected.provenance,
            }
            if command.operation is DocumentOperation.VERSION:
                family = session.get(DocumentFamilyRecord, expected.family_id)
                if family is None or family.task_id != expected.task_id:
                    raise ValueError("문서 계열이 변경되었습니다.")
                version = AttachmentVersionRecord(
                    task_id=expected.task_id,
                    family_id=expected.family_id,
                    asset_id=asset.id,
                    original_name=expected.name,
                    label=command.label.strip(),
                    memo=command.memo,
                    suggestion_number=family.next_version_number,
                    created_at=capture.captured_at,
                    metadata_revision=0,
                    **source_links,
                )
                family.next_version_number += 1
                session.add(version)
                session.flush()
                result = DocumentResult(command.job_id, asset.id, version_id=version.id)
            elif command.operation is DocumentOperation.WORK:
                slot = (
                    session.scalar(
                        select(func.max(AttachmentRecord.sort_order)).where(
                            AttachmentRecord.task_id == expected.task_id
                        )
                    )
                    or 0
                )
                work = AttachmentRecord(
                    task_id=expected.task_id,
                    family_id=expected.family_id,
                    asset_id=asset.id,
                    original_name=command.filename,
                    display_name=command.filename,
                    role="work",
                    memo="",
                    size_bytes=capture.size,
                    checksum=capture.checksum,
                    created_at=capture.captured_at,
                    sort_order=slot + 10,
                    metadata_revision=0,
                    **source_links,
                )
                session.add(work)
                session.flush()
                result = DocumentResult(command.job_id, asset.id, attachment_id=work.id)
            elif command.operation is DocumentOperation.RELEASE_ORIGINAL:
                original = session.get(AttachmentRecord, expected.id)
                family = session.get(DocumentFamilyRecord, expected.family_id)
                if (
                    original is None
                    or original.role != "original"
                    or family is None
                    or family.task_id != expected.task_id
                ):
                    raise ValueError("지정 해제할 원본/문서 계열이 변경되었습니다.")
                # Keep the already verified frozen bytes, never unfreeze them in place.
                retained_version = AttachmentVersionRecord(
                    task_id=original.task_id,
                    family_id=original.family_id,
                    asset_id=original.asset_id,
                    original_name=original.display_name,
                    label="원본 해제 전 내용",
                    memo=original.memo,
                    suggestion_number=family.next_version_number,
                    created_at=capture.captured_at,
                    metadata_revision=0,
                    **source_links,
                )
                family.next_version_number += 1
                session.add(retained_version)
                original.asset = asset
                original.role = "general"
                original.metadata_revision += 1
                session.flush()
                result = DocumentResult(
                    command.job_id,
                    asset.id,
                    attachment_id=original.id,
                    retained_version_id=retained_version.id,
                )
            else:
                original = session.get(AttachmentRecord, expected.id)
                if original is None or original.role not in {"general", "work"}:
                    raise ValueError("원본으로 지정할 편집 첨부가 변경되었습니다.")
                # Clone the old logical ownership BEFORE changing its pointer.
                retained = AttachmentRecord(
                    task_id=original.task_id,
                    family_id=original.family_id,
                    asset_id=original.asset_id,
                    role=original.role,
                    original_name=original.original_name,
                    display_name=original.display_name,
                    memo=original.memo,
                    size_bytes=original.size_bytes,
                    checksum=original.checksum,
                    created_at=original.created_at,
                    sort_order=original.sort_order,
                    detached_at=now,
                    metadata_revision=original.metadata_revision + 1,
                    source_attachment_id=original.source_attachment_id,
                    source_version_id=original.source_version_id,
                    source_payload=original.source_payload,
                )
                original.asset = asset
                original.role = "original"
                original.metadata_revision += 1
                session.flush()  # Release the old asset's UNIQUE owner before inserting retained.
                session.add(retained)
                session.flush()
                result = DocumentResult(
                    command.job_id,
                    asset.id,
                    attachment_id=original.id,
                    retained_attachment_id=retained.id,
                )
            payload = {
                "request_fingerprint": command.request_fingerprint,
                "source_fingerprint": expected.state_fingerprint,
                "result": asdict(result),
                "frozen": frozen,
                "size": capture.size,
                "checksum": capture.checksum,
            }
            session.add(
                FileOperationReceiptRecord(
                    job_id=command.job_id,
                    operation_kind=command.operation.value,
                    committed_at=now,
                    payload_version=1,
                    payload=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                )
            )
            session.flush()
        return result
