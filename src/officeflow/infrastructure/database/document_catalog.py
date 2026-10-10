"""Bounded document reads. Frozen versions use the same verified export descriptor."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC
from typing import cast

from sqlalchemy import func, select

from officeflow.domain.attachment import Attachment, AttachmentRole
from officeflow.infrastructure.database.models import (
    AttachmentRecord,
    AttachmentVersionRecord,
    DocumentFamilyRecord,
    SnapshotFileRecord,
    TaskRecord,
    TaskSnapshotRecord,
)
from officeflow.infrastructure.database.session import SessionFactory


@dataclass(frozen=True, slots=True)
class DocumentEntry:
    kind: str
    id: int
    task_id: int
    family_id: str
    revision: int
    name: str
    label: str
    memo: str
    file: Attachment
    current_source_name: str = ""
    source_name_at_creation: str = ""
    current_source_file_name: str = ""


@dataclass(frozen=True, slots=True)
class DocumentPage:
    items: tuple[DocumentEntry, ...]
    has_next: bool


class DocumentCatalog:
    def __init__(self, sessions: SessionFactory) -> None:
        self.sessions = sessions

    @staticmethod
    def _entry(row: AttachmentRecord | AttachmentVersionRecord) -> DocumentEntry:
        version = isinstance(row, AttachmentVersionRecord)
        name = row.original_name if isinstance(row, AttachmentVersionRecord) else row.display_name
        asset = row.asset
        attachment = Attachment(
            id=None if version else row.id,
            task_id=row.task_id,
            original_name=row.original_name,
            stored_name=asset.stored_name,
            relative_path=asset.relative_path,
            size_bytes=(asset.fixed_size_bytes or 0)
            if isinstance(row, AttachmentVersionRecord)
            else row.size_bytes,
            checksum=asset.fixed_checksum
            if isinstance(row, AttachmentVersionRecord)
            else row.checksum,
            created_at=row.created_at.replace(tzinfo=UTC),
            display_name=name,
            role=AttachmentRole.ORIGINAL
            if isinstance(row, AttachmentVersionRecord)
            else AttachmentRole(row.role),
            asset_id=asset.id,
            family_id=row.family_id,
            metadata_revision=row.metadata_revision,
            fixed_size_bytes=asset.fixed_size_bytes,
            fixed_checksum=asset.fixed_checksum,
            memo=row.memo,
        )
        return DocumentEntry(
            "version" if version else "attachment",
            row.id,
            row.task_id,
            row.family_id,
            row.metadata_revision,
            name,
            row.label if isinstance(row, AttachmentVersionRecord) else "",
            row.memo,
            attachment,
            source_name_at_creation=str(json.loads(row.source_payload).get("name", "")),
        )

    def get(self, kind: str, identity: int) -> DocumentEntry:
        if kind == "snapshot_file":
            with self.sessions.transaction() as session:
                snapshot_file = session.get(SnapshotFileRecord, identity)
                if (
                    snapshot_file is None
                    or snapshot_file.asset.state != "active"
                    or snapshot_file.asset.kind != "frozen"
                ):
                    raise LookupError("중요 보관 파일을 찾을 수 없습니다.")
                return self._snapshot_entry(snapshot_file)
        if kind not in {"attachment", "version"}:
            raise ValueError("첨부/보관본을 선택하세요.")
        with self.sessions.transaction() as session:
            row = cast(
                AttachmentRecord | AttachmentVersionRecord | None,
                session.get(
                    AttachmentRecord if kind == "attachment" else AttachmentVersionRecord, identity
                ),
            )
            if row is None or row.asset.state != "active":
                raise LookupError("첨부/보관본을 찾을 수 없습니다. 새로고침하세요.")
            entry = self._entry(row)
            if row.source_attachment_id is not None:
                source = session.get(AttachmentRecord, row.source_attachment_id)
                if source is not None:
                    from dataclasses import replace

                    entry = replace(
                        entry,
                        current_source_name=source.display_name,
                        current_source_file_name=source.display_name,
                    )
            elif row.source_version_id is not None:
                source_version = session.get(AttachmentVersionRecord, row.source_version_id)
                if source_version is not None:
                    from dataclasses import replace

                    entry = replace(
                        entry,
                        current_source_name=f"{source_version.label} · {source_version.original_name} (보관본)",
                        current_source_file_name=source_version.original_name,
                    )
            return entry

    @staticmethod
    def _snapshot_entry(row: SnapshotFileRecord) -> DocumentEntry:
        asset = row.asset
        if (asset.fixed_size_bytes, asset.fixed_checksum) != (
            row.fixed_size_bytes,
            row.fixed_checksum,
        ):
            raise ValueError("중요 보관 파일의 고정 기준이 일치하지 않습니다.")
        file = Attachment(
            id=None,
            task_id=row.snapshot.task_id,
            original_name=row.original_name,
            stored_name=asset.stored_name,
            relative_path=asset.relative_path,
            size_bytes=row.fixed_size_bytes,
            checksum=row.fixed_checksum,
            created_at=row.snapshot.captured_at,
            display_name=row.name,
            role=AttachmentRole.ORIGINAL,
            asset_id=asset.id,
            family_id=row.family_id,
            metadata_revision=0,
            fixed_size_bytes=row.fixed_size_bytes,
            fixed_checksum=row.fixed_checksum,
            memo=row.memo,
        )
        return DocumentEntry(
            "snapshot_file",
            row.id,
            row.snapshot.task_id,
            row.family_id,
            0,
            row.name,
            row.snapshot.label,
            row.memo,
            file,
            source_name_at_creation=row.name,
        )

    def snapshot_files(
        self, task_id: int, snapshot_id: int, *, offset: int = 0, limit: int = 50
    ) -> DocumentPage:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("보관 파일 페이지를 확인하세요.")
        with self.sessions.transaction() as session:
            rows = tuple(
                session.scalars(
                    select(SnapshotFileRecord)
                    .join(
                        TaskSnapshotRecord, TaskSnapshotRecord.id == SnapshotFileRecord.snapshot_id
                    )
                    .where(
                        SnapshotFileRecord.snapshot_id == snapshot_id,
                        TaskSnapshotRecord.task_id == task_id,
                    )
                    .order_by(SnapshotFileRecord.position, SnapshotFileRecord.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            return DocumentPage(
                tuple(self._snapshot_entry(row) for row in rows[:limit]), len(rows) > limit
            )

    def page(
        self,
        task_id: int,
        *,
        kind: str = "version",
        family_id: str | None = None,
        before: int | None = None,
        limit: int = 50,
    ) -> tuple[DocumentEntry, ...]:
        return self.page_with_more(
            task_id, kind=kind, family_id=family_id, before=before, limit=limit
        ).items

    def page_with_more(
        self,
        task_id: int,
        *,
        kind: str = "version",
        family_id: str | None = None,
        before: int | None = None,
        limit: int = 50,
    ) -> DocumentPage:
        if kind not in {"attachment", "version"} or type(limit) is not int or limit < 1:
            raise ValueError("첨부/보관본 조회 종류와 페이지 크기를 확인하세요.")
        limit = min(limit, 100)
        model = AttachmentVersionRecord if kind == "version" else AttachmentRecord
        query = select(model).where(model.task_id == task_id)
        if kind != "version":
            query = query.where(AttachmentRecord.detached_at.is_(None))
        if family_id:
            query = query.where(model.family_id == family_id)
        if before is not None:
            query = query.where(model.id < before)
        with self.sessions.transaction() as session:
            items = tuple(
                self._entry(cast(AttachmentRecord | AttachmentVersionRecord, row))
                for row in session.scalars(query.order_by(model.id.desc()).limit(limit + 1))
            )
            return DocumentPage(items[:limit], len(items) > limit)

    def counts(self, task_id: int) -> dict[str, int]:
        with self.sessions.transaction() as session:
            return {
                str(row[0]): int(row[1])
                for row in session.execute(
                    select(AttachmentVersionRecord.family_id, func.count())
                    .where(AttachmentVersionRecord.task_id == task_id)
                    .group_by(AttachmentVersionRecord.family_id)
                ).all()
            }

    def suggestion(self, family_id: str) -> str:
        with self.sessions.transaction() as session:
            row = session.get(DocumentFamilyRecord, family_id)
            if row is None:
                raise LookupError("문서 계열을 찾을 수 없습니다.")
            return f"보관본 {row.next_version_number}"

    def work_suggestion(self, task_id: int, name: str) -> str:
        from pathlib import Path

        path = Path(name)
        base = f"{path.stem} - 작업본"
        with self.sessions.transaction() as session:
            # SQL EXISTS probes, not loading all attachment names for a large task.
            for number in range(1, 101):
                candidate = f"{base}{'' if number == 1 else ' ' + str(number)}{path.suffix}"
                if not session.scalar(
                    select(AttachmentRecord.id)
                    .where(
                        AttachmentRecord.task_id == task_id,
                        AttachmentRecord.display_name == candidate,
                    )
                    .limit(1)
                ):
                    return candidate
        return f"{base}{path.suffix}"  # duplicates remain valid and never overwrite files

    def set_version_memo(self, identity: int, revision: int, memo: str) -> None:
        if len(memo) > 2000:
            raise ValueError("메모는 2,000자 이하여야 합니다.")
        with self.sessions.transaction() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = session.get(AttachmentVersionRecord, identity)
            if row is None or row.metadata_revision != revision:
                raise ValueError("보관 정보가 변경되었습니다. 입력은 유지됩니다. 새로고침하세요.")
            task = session.get(TaskRecord, row.task_id)
            if task is None or task.deleted_at is not None:
                raise ValueError("업무 복원 후 메모를 수정하세요.")
            row.memo, row.metadata_revision = memo, revision + 1
