"""Stable multi-file snapshot capture, one parent journal and one DB commit."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import uuid4

from sqlalchemy import func, select

from officeflow.application.attachment_documents import InternalRecoveryReport
from officeflow.application.attachments import AttachmentCanceledError
from officeflow.application.task_snapshots import SnapshotCommand, SnapshotDetail, SnapshotResult
from officeflow.application.tasks import TaskService
from officeflow.domain.task_history import encode, fingerprint, scope_key, task_values, utc_text
from officeflow.infrastructure.attachments.internal_storage import checked_managed_path
from officeflow.infrastructure.attachments.read_lease import (
    delete_open_file,
    exclusive_read,
    file_identity,
    protected_read,
    rename_open_file,
)
from officeflow.infrastructure.attachments.snapshot_storage import SnapshotStorage
from officeflow.infrastructure.database.document_catalog import (
    DocumentCatalog,
    DocumentEntry,
    DocumentPage,
)
from officeflow.infrastructure.database.models import (
    AttachmentRecord,
    ChecklistItemRecord,
    FileAssetRecord,
    FileOperationReceiptRecord,
    SnapshotFileRecord,
    TaskHistoryRecord,
    TaskSnapshotRecord,
)
from officeflow.infrastructure.database.session import SessionFactory
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository


class TaskSnapshotService:
    def __init__(self, sessions: SessionFactory, storage: SnapshotStorage) -> None:
        self.sessions, self.storage = sessions, storage
        self.coordinator = storage.coordinator

    def task_id(self, identity: int) -> int:
        with self.sessions.transaction() as session:
            task_id = session.scalar(
                select(TaskSnapshotRecord.task_id).where(TaskSnapshotRecord.id == identity)
            )
            if task_id is None:
                raise LookupError("중요 보관을 찾을 수 없습니다.")
            return task_id

    def suggestion(self, task_id: int) -> str:
        with self.sessions.transaction() as session:
            ordinal = (
                session.scalar(
                    select(func.max(TaskSnapshotRecord.ordinal)).where(
                        TaskSnapshotRecord.task_id == task_id
                    )
                )
                or 0
            )
            return f"업무 보관 {ordinal + 1}"

    def selection_page(self, task_id: int, kind: str, offset: int = 0) -> DocumentPage:
        from officeflow.infrastructure.database.document_catalog import DocumentPage
        from officeflow.infrastructure.database.models import AttachmentVersionRecord

        if kind not in {"attachment", "version"} or offset < 0:
            raise ValueError("파일 선택 페이지를 확인하세요.")
        model = AttachmentRecord if kind == "attachment" else AttachmentVersionRecord
        query = select(model).where(model.task_id == task_id)
        if kind == "attachment":
            query = query.where(AttachmentRecord.detached_at.is_(None)).order_by(
                AttachmentRecord.sort_order, AttachmentRecord.id
            )
        else:
            query = query.order_by(
                AttachmentVersionRecord.created_at.desc(), AttachmentVersionRecord.id.desc()
            )
        with self.sessions.transaction() as session:
            rows = tuple(session.scalars(query.offset(offset).limit(101)))
            return DocumentPage(
                tuple(
                    DocumentCatalog._entry(cast(AttachmentRecord | AttachmentVersionRecord, row))
                    for row in rows[:100]
                ),
                len(rows) > 100,
            )

    @staticmethod
    def request(command: SnapshotCommand) -> str:
        return fingerprint(asdict(command))

    def committed(self, command: SnapshotCommand) -> SnapshotResult | None:
        with self.sessions.transaction() as session:
            receipt = session.get(FileOperationReceiptRecord, command.job_id)
            if receipt is None:
                return None
            raw = json.loads(receipt.payload)
            if (
                receipt.operation_kind != "task_snapshot"
                or receipt.payload_version != 1
                or raw.get("request") != self.request(command)
            ):
                raise ValueError("같은 보관 ID에 다른 요청을 사용할 수 없습니다.")
            result = SnapshotResult(command.job_id, raw["snapshot_id"], True)
            if session.get(TaskSnapshotRecord, result.snapshot_id) is None:
                result = replace(
                    result, warning="이미 보관했으나 이후 삭제되었습니다. 다시 생성하지 않았습니다."
                )
            return result

    def _state(
        self, sessions: SessionFactory, command: SnapshotCommand
    ) -> tuple[dict[str, Any], tuple[DocumentEntry, ...], str]:
        tasks = TaskService(SqlAlchemyTaskRepository(sessions))
        task = tasks.get(command.task_id)  # trash is not a writable source
        occurrence = (
            tasks._validated_occurrence(task, command.occurrence_start)
            if command.occurrence_start
            else None
        )
        info = {
            "values": task_values(task, occurrence),
            "is_pinned": task.is_pinned,
            "created_at": utc_text(task.created_at),
            "checklist": [],
            "payload_version": 1,
        }
        with sessions.transaction() as session:
            info["checklist"] = [
                dict(
                    id=row.id,
                    content=row.content,
                    is_done=row.is_done,
                    position=row.position,
                    completed_at=utc_text(row.completed_at),
                )
                for row in session.scalars(
                    select(ChecklistItemRecord)
                    .where(ChecklistItemRecord.task_id == task.id)
                    .order_by(ChecklistItemRecord.position, ChecklistItemRecord.id)
                )
            ]
            common_id = session.scalar(
                select(func.max(TaskHistoryRecord.id)).where(
                    TaskHistoryRecord.task_id == task.id, TaskHistoryRecord.scope_key == "task"
                )
            )
            occurrence_id = (
                session.scalar(
                    select(func.max(TaskHistoryRecord.id)).where(
                        TaskHistoryRecord.task_id == task.id,
                        TaskHistoryRecord.scope_key == scope_key(command.occurrence_start),
                    )
                )
                if command.occurrence_start
                else None
            )
            catalog = DocumentCatalog(sessions)
            entries = tuple(catalog.get(item.kind, item.id) for item in command.selections)
            if any(entry.task_id != command.task_id for entry in entries):
                raise ValueError("다른 업무의 파일은 함께 보관할 수 없습니다.")
            order = {
                row.id: row.sort_order
                for row in session.scalars(
                    select(AttachmentRecord).where(
                        AttachmentRecord.id.in_([e.id for e in entries if e.kind == "attachment"])
                    )
                )
            }
            entries = tuple(
                sorted(
                    entries,
                    key=lambda e: (
                        (0, order.get(e.id, 0), e.id) if e.kind == "attachment" else (1, -e.id, 0)
                    ),
                )
            )
            metadata = [
                dict(
                    kind=e.kind,
                    id=e.id,
                    asset=e.file.asset_id,
                    family=e.family_id,
                    revision=e.revision,
                    name=e.name,
                    original_name=e.file.original_name,
                    role=e.file.role.value,
                    label=e.label,
                    memo=e.memo,
                    order=order.get(e.id),
                    path=e.file.relative_path,
                    fixed_size=e.file.fixed_size_bytes,
                    fixed_checksum=e.file.fixed_checksum,
                )
                for e in entries
            ]
        return (
            info,
            entries,
            fingerprint({"info": info, "history": [common_id, occurrence_id], "files": metadata}),
        )

    def create(
        self,
        command: SnapshotCommand,
        *,
        canceled: Callable[[], bool] = lambda: False,
        progress: Callable[[int, int], None] | None = None,
    ) -> SnapshotResult:
        with self.coordinator.job(canceled):
            previous = self.committed(command)
            if previous is not None:
                return previous
            if self.storage.folder(command.job_id).exists():
                raise ValueError(
                    "중단된 보관 작업이 있습니다. 자료를 확인하고 정리한 뒤 새 보관을 시작하세요."
                )
            try:
                return self._capture(command, canceled, progress)
            except BaseException:
                if self.storage.folder(command.job_id).exists():
                    self.storage.protect_pending(command.job_id)
                raise

    def _capture(
        self,
        command: SnapshotCommand,
        canceled: Callable[[], bool],
        progress: Callable[[int, int], None] | None,
    ) -> SnapshotResult:
        owner = self.storage.owner(command.job_id)
        with ExitStack() as stack:
            with self.coordinator.gate, self.sessions.read_snapshot() as snapshot:
                _initial, sources, _initial_token = self._state(snapshot, command)
                unique = {entry.file.asset_id: entry for entry in sources}
                for asset_id in sorted(unique):
                    stack.enter_context(
                        self.coordinator.pin(unique[asset_id].file.relative_path, owner=owner)
                    )
            handles: dict[str, BinaryIO] = {}
            for asset_id in sorted(unique):
                path = checked_managed_path(
                    self.storage.root, unique[asset_id].file.relative_path, file_required=True
                )
                handles[asset_id] = stack.enter_context(protected_read(path))
            with self.sessions.read_snapshot() as snapshot:
                info, entries, token = self._state(snapshot, command)
            if {(e.kind, e.id, e.file.asset_id, e.file.relative_path) for e in entries} != {
                (e.kind, e.id, e.file.asset_id, e.file.relative_path) for e in sources
            }:
                raise ValueError("파일 연결이 변경되었습니다. 다시 보관하세요.")
            captured = datetime.now(UTC)
            assets: list[dict[str, Any]] = []
            mapping: dict[str, dict[str, Any]] = {}
            for entry in entries:
                if entry.file.asset_id in mapping:
                    continue
                original = entry.file.asset_id
                identity = original if entry.file.is_frozen else uuid4().hex
                suffix = Path(entry.name).suffix.lower()
                import re

                suffix = suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix) else ""
                item: dict[str, Any] = dict(
                    id=identity,
                    new=not entry.file.is_frozen,
                    source=entry.file.relative_path,
                    source_asset=original,
                    path=entry.file.relative_path
                    if entry.file.is_frozen
                    else f"frozen/{identity[:2]}/{identity[2:4]}/{identity}{suffix}",
                    part=f".operations/snapshot-{command.job_id}/{identity}.part"
                    if not entry.file.is_frozen
                    else None,
                    size=None,
                    checksum=None,
                )
                mapping[original] = item
                assets.append(item)
            payload = dict(
                format_version=1,
                operation_kind="task_snapshot",
                job_id=command.job_id,
                task_id=command.task_id,
                request=self.request(command),
                token=token,
                command=json.loads(encode(asdict(command))),
                captured_at=utc_text(captured),
                state="copying",
                assets=assets,
            )
            folder = self.storage.folder(command.job_id)
            folder.parent.mkdir(exist_ok=True, parents=True)
            folder.mkdir(exist_ok=False)
            self.storage.write(payload)
            self.storage.checkpoint("journal")
            for index, item in enumerate(assets):
                if canceled():
                    raise AttachmentCanceledError("보관을 취소했습니다.")
                stream = handles[item["source_asset"]]
                if item["new"]:
                    part = checked_managed_path(self.storage.root, item["part"])
                    with part.open("xb") as output:
                        item["size"], item["checksum"] = self.storage.digest(
                            stream, canceled, output
                        )
                    lock = stack.enter_context(exclusive_read(part))
                    if self.storage.digest(lock, canceled) != (item["size"], item["checksum"]):
                        raise ValueError("준비 파일이 변경되었습니다.")
                    item["identity"] = list(file_identity(os.fstat(lock.fileno())))
                    item["handle"] = lock
                else:
                    item["size"], item["checksum"] = self.storage.digest(stream, canceled)
                    source = unique[item["source_asset"]].file
                    if (item["size"], item["checksum"]) != (
                        source.fixed_size_bytes,
                        source.fixed_checksum,
                    ):
                        raise ValueError("고정 보관 파일의 내용이 달라 보관하지 않았습니다.")
                self._write(payload)
                self.storage.checkpoint("copied")
                if progress:
                    progress(index + 1, len(assets))
            payload["state"] = "prepared"
            self._write(payload)
            self.storage.checkpoint("prepared")
            if canceled():
                raise AttachmentCanceledError("보관을 취소했습니다.")
            with self.coordinator.gate, self.sessions.write_unit_of_work() as work:
                _fresh, _files, fresh_token = self._state(work, command)
                if fresh_token != token:
                    raise ValueError("자료가 보관 중 변경되었습니다. 새로 보관하세요.")
                with work.transaction() as session:
                    if session.get(FileOperationReceiptRecord, command.job_id) is not None:
                        raise ValueError("같은 보관 작업이 이미 확정되었습니다.")
                    for item in assets:
                        if item["new"]:
                            destination = checked_managed_path(self.storage.root, item["path"])
                            destination.parent.mkdir(exist_ok=True, parents=True)
                            rename_open_file(item["handle"], destination)
                            session.add(
                                FileAssetRecord(
                                    id=item["id"],
                                    kind="frozen",
                                    state="active",
                                    stored_name=destination.name,
                                    relative_path=item["path"],
                                    created_at=captured,
                                    fixed_size_bytes=item["size"],
                                    fixed_checksum=item["checksum"],
                                    frozen_at=datetime.now(UTC),
                                )
                            )
                    payload["state"] = "published"
                    self._write(payload)
                    self.storage.checkpoint("published")
                    ordinal = (
                        session.scalar(
                            select(func.max(TaskSnapshotRecord.ordinal)).where(
                                TaskSnapshotRecord.task_id == command.task_id
                            )
                        )
                        or 0
                    ) + 1
                    row = TaskSnapshotRecord(
                        task_id=command.task_id,
                        scope="occurrence" if command.occurrence_start else "task",
                        scope_key=scope_key(command.occurrence_start),
                        occurrence_start=command.occurrence_start,
                        label=command.label,
                        ordinal=ordinal,
                        captured_at=captured,
                        committed_at=datetime.now(UTC),
                        job_id=command.job_id,
                        request_fingerprint=self.request(command),
                        payload_version=1,
                        info_payload=encode(info),
                        file_count=len(entries),
                        summary=command.label,
                    )
                    session.add(row)
                    session.flush()
                    for position, entry in enumerate(entries):
                        item = mapping[entry.file.asset_id]
                        session.add(
                            SnapshotFileRecord(
                                snapshot_id=row.id,
                                asset_id=item["id"],
                                position=position,
                                name=entry.name,
                                original_name=entry.file.original_name,
                                role=entry.file.role.value,
                                memo=entry.memo,
                                family_id=entry.family_id,
                                source_kind=entry.kind,
                                source_id=entry.id,
                                source_payload=encode(
                                    {
                                        "kind": entry.kind,
                                        "id": entry.id,
                                        "name": entry.name,
                                        "label": entry.label,
                                        "family_id": entry.family_id,
                                    }
                                ),
                                fixed_size_bytes=item["size"],
                                fixed_checksum=item["checksum"],
                                payload_version=1,
                            )
                        )
                    result = SnapshotResult(command.job_id, row.id)
                    session.add(
                        FileOperationReceiptRecord(
                            job_id=command.job_id,
                            operation_kind="task_snapshot",
                            committed_at=datetime.now(UTC),
                            payload_version=1,
                            payload=encode(
                                {"request": self.request(command), "snapshot_id": row.id}
                            ),
                        )
                    )
            self.storage.checkpoint("committed")
        self.sessions.task_events.publish(command.task_id)
        try:
            self.storage.finish(command.job_id)
            self.storage.checkpoint("finished")
        except Exception:
            return replace(
                result, warning="중요 보관은 완료했습니다. 남은 작업 자료 정리가 필요합니다."
            )
        return result

    def _write(self, payload: dict[str, Any]) -> None:
        clean = dict(payload)
        clean["assets"] = [
            {key: value for key, value in item.items() if key != "handle"}
            for item in payload["assets"]
        ]
        self.storage.write(clean)

    def get(self, task_id: int, identity: int) -> SnapshotDetail:
        with self.sessions.transaction() as session:
            row = session.get(TaskSnapshotRecord, identity)
            if row is None or row.task_id != task_id or row.payload_version != 1:
                raise LookupError("중요 보관을 찾을 수 없습니다.")
            return SnapshotDetail(
                row.id,
                row.task_id,
                row.label,
                row.captured_at,
                row.committed_at,
                row.occurrence_start,
                json.loads(row.info_payload),
                row.file_count,
            )

    def recover(
        self, job_id: str, canceled: Callable[[], bool] = lambda: False, *, background: bool = False
    ) -> InternalRecoveryReport:
        with self.coordinator.job(canceled, background=background):
            raw = self.storage.read(job_id)
            with self.sessions.read_snapshot() as snapshot, snapshot.transaction() as session:
                receipt = session.get(FileOperationReceiptRecord, job_id)
                if receipt is None:
                    self.storage.protect_pending(job_id)
                    return InternalRecoveryReport(
                        0, 1, ("미확정 중요 보관 자료를 보존했습니다. 이력·보관에서 확인하세요.",)
                    )
                saved = json.loads(receipt.payload)
                if (
                    receipt.operation_kind != "task_snapshot"
                    or saved.get("request") != raw["request"]
                ):
                    raise ValueError("보관 완료 기록과 작업 기록이 다릅니다.")
                row = session.get(TaskSnapshotRecord, saved["snapshot_id"])
                if row is None:
                    raise ValueError("확정 보관 자료를 찾을 수 없어 작업 기록을 보존했습니다.")
                for item in raw["assets"]:
                    asset = session.get(FileAssetRecord, item["id"])
                    if (
                        asset is None
                        or asset.relative_path != item["path"]
                        or asset.fixed_checksum != item["checksum"]
                        or asset.fixed_size_bytes != item["size"]
                    ):
                        raise ValueError("확정 보관 자산의 연결을 확인하세요.")
                    if (
                        session.scalar(
                            select(SnapshotFileRecord.id)
                            .where(
                                SnapshotFileRecord.snapshot_id == row.id,
                                SnapshotFileRecord.asset_id == asset.id,
                            )
                            .limit(1)
                        )
                        is None
                    ):
                        raise ValueError("확정 보관의 파일 연결이 없습니다.")
            with ExitStack() as stack:
                for item in sorted(raw["assets"], key=lambda value: value["id"]):
                    stack.enter_context(
                        self.coordinator.pin(item["path"], owner=self.storage.owner(job_id))
                    )
                    path = checked_managed_path(self.storage.root, item["path"], file_required=True)
                    stream = stack.enter_context(protected_read(path))
                    if self.storage.digest(stream, canceled) != (item["size"], item["checksum"]):
                        raise ValueError(
                            "확정 보관 파일의 검증값이 다릅니다. 작업 기록을 보존했습니다."
                        )
                self.storage.finish(job_id)
            return InternalRecoveryReport(1, 0, ())

    def pending(self, task_id: int, *, limit: int = 200) -> tuple[tuple[str, str], ...]:
        """Bounded journal-only inspection; never hash every stored file on startup."""
        directory = self.storage.root / ".operations"
        found: list[tuple[str, str]] = []
        if not directory.exists():
            return ()
        inspected = 0
        for folder in directory.iterdir():
            if inspected >= limit:
                break
            if not folder.name.startswith("snapshot-"):
                continue
            inspected += 1
            identity = folder.name.removeprefix("snapshot-")
            try:
                raw = self.storage.read(identity)
                if raw["task_id"] == task_id:
                    found.append((identity, raw["command"]["label"]))
            except (OSError, ValueError, KeyError, TypeError):
                continue  # unknown journals remain protected; data-management inspection explains them
        return tuple(found)

    def discard_pending(
        self,
        task_id: int,
        job_id: str,
        *,
        approved: bool = False,
        canceled: Callable[[], bool] = lambda: False,
    ) -> None:
        """Only remove verified, unregistered outputs owned by this parent. Never sources."""
        if not approved:
            raise ValueError("미완료 보관의 준비 파일 정리에 동의하세요.")
        with self.coordinator.job(canceled), ExitStack() as stack:
            raw = self.storage.read(job_id)
            if raw["task_id"] != task_id:
                raise ValueError("다른 업무의 보관 작업입니다.")
            owned = [item for item in raw["assets"] if item["new"]]
            handles: list[BinaryIO] = []
            with self.coordinator.gate, self.sessions.transaction() as session:
                if session.get(FileOperationReceiptRecord, job_id) is not None:
                    raise ValueError("완료된 보관은 상세의 보관 삭제를 이용하세요.")
                for item in owned:
                    if (
                        session.get(FileAssetRecord, item["id"]) is not None
                        or session.scalar(
                            select(FileAssetRecord.id)
                            .where(FileAssetRecord.relative_path.in_((item["path"], item["part"])))
                            .limit(1)
                        )
                        is not None
                    ):
                        raise ValueError("등록된 자산이 있어 준비 파일을 정리하지 않았습니다.")
                    for relative in (item["path"], item["part"]):
                        stack.enter_context(
                            self.coordinator.pin(relative, owner=self.storage.owner(job_id))
                        )
            for item in owned:
                candidates = [
                    checked_managed_path(self.storage.root, relative)
                    for relative in (item["part"], item["path"])
                ]
                present = [path for path in candidates if path.exists()]
                if len(present) > 1:
                    raise ValueError("같은 보관 파일이 두 위치에 있어 자동 정리하지 않았습니다.")
                if not present:
                    continue
                if item.get("identity") is None or item.get("checksum") is None:
                    raise ValueError(
                        "복사 도중 중단된 파일은 검증할 수 없습니다. 데이터 관리에서 위치를 확인하세요."
                    )
                handle = stack.enter_context(exclusive_read(present[0]))
                if list(file_identity(os.fstat(handle.fileno()))) != item[
                    "identity"
                ] or self.storage.digest(handle, canceled) != (item["size"], item["checksum"]):
                    raise ValueError("준비 파일이 변경되어 보존했습니다. 직접 확인하세요.")
                handles.append(handle)
            if canceled():
                raise AttachmentCanceledError("정리를 취소했습니다.")
            with self.coordinator.gate, self.sessions.transaction() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                if (
                    session.get(FileOperationReceiptRecord, job_id) is not None
                    or session.scalar(
                        select(FileAssetRecord.id)
                        .where(FileAssetRecord.id.in_([item["id"] for item in owned]))
                        .limit(1)
                    )
                    is not None
                ):
                    raise ValueError("보관 상태가 바뀌어 정리하지 않았습니다.")
                for handle in handles:
                    delete_open_file(handle)
            # On Windows disposition deletes the prepared path only after handle close.
            stack.close()
            self.storage.finish(job_id)
