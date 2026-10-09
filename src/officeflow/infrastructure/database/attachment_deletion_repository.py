"""One short transaction for references, pending assets, and delete receipts."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import delete, false, select
from sqlalchemy.orm import Session

from officeflow.application.attachment_deletion import (
    DeleteAsset,
    DeleteCommand,
    DeleteCommit,
    DeletePlan,
    DeleteSelection,
    DeleteTarget,
)
from officeflow.application.attachment_documents import fingerprint
from officeflow.application.attachments import AttachmentOperationError
from officeflow.infrastructure.database.file_references import (
    assert_supported_asset_references,
    asset_reference_count,
)
from officeflow.infrastructure.database.models import (
    AttachmentRecord,
    AttachmentVersionRecord,
    FileAssetRecord,
    FileOperationReceiptRecord,
    TaskRecord,
    WorkLogRecord,
)
from officeflow.infrastructure.database.session import SessionFactory


class SqlAlchemyDeletionRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    @staticmethod
    def _asset_receipt_id(command: DeleteCommand, asset: DeleteAsset) -> str:
        return uuid5(NAMESPACE_URL, "officeflow:delete:" + command.job_id + ":" + asset.id).hex

    def plan(self, target: DeleteTarget | DeleteSelection) -> DeletePlan:
        with self._sessions.read_snapshot() as snapshot, snapshot.transaction() as session:
            return self._plan(session, target)

    @staticmethod
    def _plan(session: Session, target: DeleteTarget | DeleteSelection) -> DeletePlan:
        assert_supported_asset_references(session)
        attachments = select(AttachmentRecord)
        versions = select(AttachmentVersionRecord)
        if isinstance(target, DeleteSelection):
            attachments = attachments.where(
                AttachmentRecord.id.in_(
                    [item.id for item in target.items if item.kind == "attachment"]
                )
            )
            versions = versions.where(
                AttachmentVersionRecord.id.in_(
                    [item.id for item in target.items if item.kind == "version"]
                )
            )
        elif target.kind == "attachment":
            attachments = attachments.where(AttachmentRecord.id == target.id)
            versions = versions.where(false())
        elif target.kind == "version":
            attachments = attachments.where(false())
            versions = versions.where(AttachmentVersionRecord.id == target.id)
        else:
            attachments = attachments.where(AttachmentRecord.task_id == target.id)
            versions = versions.where(AttachmentVersionRecord.task_id == target.id)
        attached = tuple(session.scalars(attachments.order_by(AttachmentRecord.id)))
        saved = tuple(session.scalars(versions.order_by(AttachmentVersionRecord.id)))
        if isinstance(target, DeleteSelection) and (
            len(attached) + len(saved) != len(target.items)
            or any(row.task_id != target.id for row in attached)
            or any(row.task_id != target.id for row in saved)
        ):
            raise AttachmentOperationError(
                "선택 파일이 없거나 다른 업무의 자료입니다. 선택을 다시 확인하세요."
            )
        task_id = (
            target.id
            if target.kind in {"task", "selection"}
            else (attached[0].task_id if attached else saved[0].task_id if saved else None)
        )
        task = session.get(TaskRecord, task_id) if task_id is not None else None
        if (
            task is None
            or (target.kind == "task" and task.deleted_at is None)
            or (target.kind != "task" and not attached and not saved)
        ):
            raise LookupError(
                "삭제할 자료를 찾을 수 없습니다. 업무 영구 삭제는 휴지통에서만 가능합니다."
            )
        if target.kind != "task" and task.deleted_at is not None:
            raise AttachmentOperationError(
                "휴지통 업무는 조회만 가능합니다. 먼저 업무를 복원하세요."
            )
        # Use subqueries rather than N per-file lookups or a huge SQLite IN parameter list.
        asset_ids = attachments.with_only_columns(AttachmentRecord.asset_id).union(
            versions.with_only_columns(AttachmentVersionRecord.asset_id)
        )
        refs_a = tuple(
            session.execute(
                select(
                    AttachmentRecord.asset_id,
                    AttachmentRecord.id,
                    AttachmentRecord.task_id,
                    AttachmentRecord.metadata_revision,
                )
                .where(AttachmentRecord.asset_id.in_(asset_ids))
                .order_by(AttachmentRecord.id)
            )
        )
        refs_v = tuple(
            session.execute(
                select(
                    AttachmentVersionRecord.asset_id,
                    AttachmentVersionRecord.id,
                    AttachmentVersionRecord.task_id,
                    AttachmentVersionRecord.metadata_revision,
                )
                .where(AttachmentVersionRecord.asset_id.in_(asset_ids))
                .order_by(AttachmentVersionRecord.id)
            )
        )
        selected_a = {row.id for row in attached}
        selected_v = {row.id for row in saved}
        outside = {row.asset_id for row in refs_a if row.id not in selected_a} | {
            row.asset_id for row in refs_v if row.id not in selected_v
        }
        assets = tuple(
            session.scalars(
                select(FileAssetRecord)
                .where(FileAssetRecord.id.in_(asset_ids))
                .order_by(FileAssetRecord.id)
            )
        )
        if any(row.state != "active" for row in assets):
            raise AttachmentOperationError("정리 중인 자산은 새 삭제 대상으로 사용할 수 없습니다.")
        logs = (
            tuple(
                session.scalars(
                    select(WorkLogRecord.id)
                    .where(WorkLogRecord.task_id == task.id)
                    .order_by(WorkLogRecord.id)
                )
            )
            if target.kind == "task"
            else ()
        )
        kept_versions = (
            0
            if target.kind == "task"
            else len(
                tuple(
                    session.scalars(
                        select(AttachmentVersionRecord.id).where(
                            AttachmentVersionRecord.task_id == task.id,
                            AttachmentVersionRecord.id.not_in(selected_v),
                        )
                    )
                )
            )
        )
        # User fields only; file observation changes do not invalidate confirmation.
        logical = [
            [
                r.id,
                r.asset_id,
                r.family_id,
                r.metadata_revision,
                r.display_name,
                r.role,
                r.memo,
                r.sort_order,
                str(r.detached_at),
            ]
            for r in attached
        ]
        archived = [
            [r.id, r.asset_id, r.family_id, r.metadata_revision, r.label, r.memo] for r in saved
        ]
        task_state = [task.id, str(task.deleted_at)]
        if target.kind == "task":
            task_state += [
                str(getattr(task, column.name)) for column in TaskRecord.__table__.columns
            ]
        descriptors = tuple(
            DeleteAsset(row.id, row.relative_path, row.kind, row.id not in outside)
            for row in assets
        )
        state = {
            "target": asdict(target),
            "task": task_state,
            "attachments": logical,
            "versions": archived,
            "assets": [asdict(a) for a in descriptors],
            "refs_a": [list(r) for r in refs_a],
            "refs_v": [list(r) for r in refs_v],
            "logs": logs,
            "kept_versions": kept_versions,
        }
        return DeletePlan(
            target,
            fingerprint(state),
            descriptors,
            len(attached),
            len(saved),
            kept_versions,
            len(logs),
        )

    def committed(self, command: DeleteCommand) -> DeleteCommit | None:
        with self._sessions.transaction() as session:
            return self._committed(session, command)

    @staticmethod
    def _committed(session: Session, command: DeleteCommand) -> DeleteCommit | None:
        receipt = session.get(FileOperationReceiptRecord, command.job_id)
        if receipt is None:
            return None
        if receipt.operation_kind != "delete_assets" or receipt.payload_version != 1:
            raise ValueError("다른 종류의 파일 작업에 사용한 ID입니다.")
        raw = json.loads(receipt.payload)
        if raw.get("request") != command.request_fingerprint:
            raise ValueError("같은 작업 ID로 다른 삭제 요청을 실행할 수 없습니다.")
        assets = tuple(DeleteAsset(**value) for value in raw["assets"])
        if len({asset.id for asset in assets}) != len(assets):
            raise ValueError("삭제 완료 기록의 자산이 중복되었습니다.")
        counts = [raw[key] for key in ("attachment_count", "version_count", "work_log_count")]
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("삭제 완료 기록의 개수가 올바르지 않습니다.")
        return DeleteCommit(command, assets, *counts)

    def finalize(self, command: DeleteCommand, plan: DeletePlan) -> DeleteCommit:
        with self._sessions.write_unit_of_work() as work, work.transaction() as session:
            if self._committed(session, command) is not None:
                raise AttachmentOperationError(
                    "이미 확정된 삭제입니다. 기존 작업 결과를 확인하세요."
                )
            current = self._plan(session, command.target)
            if current != plan or current.fingerprint != command.plan_fingerprint:
                raise AttachmentOperationError(
                    "삭제 범위가 변경되었습니다. 자료를 복구하고 다시 확인하세요."
                )
            if (
                command.target.kind == "task"
                and current.version_count
                and not command.include_versions
            ):
                raise AttachmentOperationError("보관본 삭제 범위를 확인하지 않았습니다.")
            if isinstance(command.target, DeleteSelection):
                session.execute(
                    delete(AttachmentRecord).where(
                        AttachmentRecord.id.in_(
                            [item.id for item in command.target.items if item.kind == "attachment"]
                        )
                    )
                )
                session.execute(
                    delete(AttachmentVersionRecord).where(
                        AttachmentVersionRecord.id.in_(
                            [item.id for item in command.target.items if item.kind == "version"]
                        )
                    )
                )
            elif command.target.kind == "attachment":
                session.execute(
                    delete(AttachmentRecord).where(AttachmentRecord.id == command.target.id)
                )
            elif command.target.kind == "version":
                session.execute(
                    delete(AttachmentVersionRecord).where(
                        AttachmentVersionRecord.id == command.target.id
                    )
                )
            else:
                session.execute(delete(TaskRecord).where(TaskRecord.id == command.target.id))
            session.flush()
            for asset in plan.assets:
                count = asset_reference_count(session, asset.id)
                if asset.removable != (count == 0):
                    raise AttachmentOperationError("파일 참조가 변경되어 삭제하지 않았습니다.")
                if asset.removable:
                    row = session.get(FileAssetRecord, asset.id)
                    assert row is not None
                    row.state = "pending_delete"
                    # A small PK-indexed receipt prevents reparsing the entire
                    # N-asset parent payload for every physical cleanup step.
                    session.add(
                        FileOperationReceiptRecord(
                            job_id=self._asset_receipt_id(command, asset),
                            operation_kind="delete_asset",
                            committed_at=datetime.now(UTC),
                            payload_version=1,
                            payload=json.dumps(
                                {
                                    "parent": command.job_id,
                                    "request": command.request_fingerprint,
                                    "asset": asdict(asset),
                                },
                                ensure_ascii=False,
                            ),
                        )
                    )
            commit = DeleteCommit(
                command, plan.assets, plan.attachment_count, plan.version_count, plan.work_log_count
            )
            session.add(
                FileOperationReceiptRecord(
                    job_id=command.job_id,
                    operation_kind="delete_assets",
                    committed_at=datetime.now(UTC),
                    payload_version=1,
                    payload=json.dumps(
                        {
                            "request": command.request_fingerprint,
                            "assets": [asdict(a) for a in plan.assets],
                            "attachment_count": plan.attachment_count,
                            "version_count": plan.version_count,
                            "work_log_count": plan.work_log_count,
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            session.flush()
            return commit

    @staticmethod
    def _state(session: Session, asset: DeleteAsset) -> str:
        assert_supported_asset_references(session)
        row = session.get(FileAssetRecord, asset.id)
        if row is None:
            # A path re-registered under another ID is not an absent asset.
            if (
                session.scalar(
                    select(FileAssetRecord.id).where(FileAssetRecord.relative_path == asset.path)
                )
                is not None
            ):
                raise AttachmentOperationError("다른 자산이 같은 경로를 사용하고 있습니다.")
            return "absent"
        if row.relative_path != asset.path or row.kind != asset.kind:
            raise AttachmentOperationError("파일 자산의 식별 정보가 변경되었습니다.")
        count = asset_reference_count(session, asset.id)
        if row.state == "active" and count:
            return "live"
        if row.state == "pending_delete" and not count:
            return "pending"
        raise AttachmentOperationError("파일 참조/삭제 상태가 일관되지 않아 보존했습니다.")

    def recovery_state(self, asset: DeleteAsset) -> str:
        with self._sessions.read_snapshot() as snapshot, snapshot.transaction() as session:
            return self._state(session, asset)

    def finish_asset(self, commit: DeleteCommit, asset: DeleteAsset) -> None:
        with self._sessions.write_unit_of_work() as work, work.transaction() as session:
            receipt = session.get(
                FileOperationReceiptRecord, self._asset_receipt_id(commit.command, asset)
            )
            expected = {
                "parent": commit.command.job_id,
                "request": commit.command.request_fingerprint,
                "asset": asdict(asset),
            }
            if (
                receipt is None
                or receipt.operation_kind != "delete_asset"
                or receipt.payload_version != 1
                or json.loads(receipt.payload) != expected
                or not asset.removable
            ):
                raise AttachmentOperationError("삭제 완료 기록과 자산이 일치하지 않습니다.")
            state = self._state(session, asset)
            if state == "live":
                raise AttachmentOperationError("보존 참조가 남은 파일은 정리할 수 없습니다.")
            if state == "pending":
                session.execute(delete(FileAssetRecord).where(FileAssetRecord.id == asset.id))
