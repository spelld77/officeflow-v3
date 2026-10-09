"""One reference contract for deletion, backup, and managed-orphan decisions."""

import sqlite3

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from officeflow.infrastructure.database.models import (
    AttachmentRecord,
    AttachmentVersionRecord,
    FileAssetRecord,
)

_SUPPORTED_REFERENCES = {("attachments", ("asset_id",)), ("attachment_versions", ("asset_id",))}


def assert_supported_sqlite_asset_references(connection: sqlite3.Connection) -> None:
    for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        quoted = '"' + table.replace('"', '""') + '"'
        references: dict[int, list[tuple[int, str]]] = {}
        for row in connection.execute(f"PRAGMA foreign_key_list({quoted})"):
            if row[2] == "file_assets":
                references.setdefault(row[0], []).append((row[1], row[3]))
        for columns in references.values():
            if (table, tuple(name for _, name in sorted(columns))) not in _SUPPORTED_REFERENCES:
                raise ValueError(
                    "지원하지 않는 파일 보존 참조가 있습니다. 파일을 변경하지 않았습니다."
                )


def assert_supported_asset_references(session: Session) -> None:
    inspector = inspect(session.connection())
    for table in inspector.get_table_names():
        for reference in inspector.get_foreign_keys(table):
            if (
                reference["referred_table"] == "file_assets"
                and (table, tuple(reference["constrained_columns"])) not in _SUPPORTED_REFERENCES
            ):
                raise ValueError(
                    "지원하지 않는 파일 보존 참조가 있습니다. 파일을 변경하지 않았습니다."
                )


def asset_reference_count(
    session: Session, asset_id: str, *, exclude_attachment: int | None = None
) -> int:
    attached = (
        select(func.count())
        .select_from(AttachmentRecord)
        .where(AttachmentRecord.asset_id == asset_id)
    )
    if exclude_attachment is not None:
        attached = attached.where(AttachmentRecord.id != exclude_attachment)
    versions = (
        select(func.count())
        .select_from(AttachmentVersionRecord)
        .where(AttachmentVersionRecord.asset_id == asset_id)
    )
    return int(session.scalar(attached) or 0) + int(session.scalar(versions) or 0)


def asset_paths_for_task(session: Session, task_id: int) -> tuple[str, ...]:
    attached = (
        select(FileAssetRecord.relative_path)
        .join(AttachmentRecord, AttachmentRecord.asset_id == FileAssetRecord.id)
        .where(AttachmentRecord.task_id == task_id)
    )
    versions = (
        select(FileAssetRecord.relative_path)
        .join(AttachmentVersionRecord, AttachmentVersionRecord.asset_id == FileAssetRecord.id)
        .where(AttachmentVersionRecord.task_id == task_id)
    )
    return tuple(session.scalars(attached.union(versions)))
