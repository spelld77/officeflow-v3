from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import ColumnElement

from officeflow.infrastructure.database.base import Base
from officeflow.infrastructure.database.types import UTCDateTime


class TaskRecord(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("length(trim(title)) > 0", name="title_not_blank"),
        CheckConstraint("ends_at IS NULL OR starts_at IS NOT NULL", name="end_requires_start"),
        CheckConstraint("ends_at IS NULL OR ends_at > starts_at", name="end_after_start"),
        CheckConstraint(
            "status IN ('active','pending','completed','canceled','archived')",
            name="valid_status",
        ),
        CheckConstraint(
            "priority IN ('normal','attention','important','urgent')",
            name="valid_priority",
        ),
        Index("ix_tasks_status_deleted_starts", "status", "deleted_at", "starts_at"),
        Index("ix_tasks_status_priority_updated", "status", "priority", "updated_at"),
        Index("ix_tasks_starts_ends", "starts_at", "ends_at"),
        Index("ix_tasks_pinned_status_updated", "is_pinned", "status", "updated_at"),
        Index("ix_tasks_active_schedule", "deleted_at", "status", "ends_at", "starts_at"),
        Index("ix_tasks_completed_at", "deleted_at", "status", "completed_at"),
        Index("ix_tasks_deleted_updated", "deleted_at", "updated_at"),
        Index("ix_tasks_recurrence_window", "recurrence_until", "starts_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Keep the existing schema and imported provenance compatible with older backups.
    legacy_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    priority: Mapped[str] = mapped_column(String(16), default="normal")
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    starts_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Seoul")
    recurrence_rule: Mapped[str | None] = mapped_column(Text)
    recurrence_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    result_note: Mapped[str] = mapped_column(Text, default="")
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class TaskHistoryRecord(Base):
    __tablename__ = "task_history"
    __table_args__ = (
        UniqueConstraint("task_id", "command_id", "scope_key", "event_type"),
        CheckConstraint("scope IN ('task','occurrence')", name="history_scope"),
        CheckConstraint("event_type IN ('created','baseline','change')", name="history_event"),
        CheckConstraint("payload_version=1", name="history_payload_version"),
        CheckConstraint(
            "(scope='task' AND scope_key='task' AND occurrence_start IS NULL) OR (scope='occurrence' AND scope_key LIKE 'occ:%' AND occurrence_start IS NOT NULL)",
            name="history_scope_values",
        ),
        Index("ix_task_history_time", "task_id", "recorded_at", "id"),
        Index("ix_task_history_scope_time", "task_id", "scope_key", "recorded_at", "id"),
        Index("ix_task_history_scope_id", "task_id", "scope_key", "id"),
        Index(
            "ux_task_history_initial",
            "task_id",
            "scope_key",
            unique=True,
            sqlite_where=text("event_type IN ('created','baseline')"),
        ),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(String(16))
    scope_key: Mapped[str] = mapped_column(String(64))
    occurrence_start: Mapped[datetime | None] = mapped_column(UTCDateTime())
    command_id: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(16))
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime())
    changed_fields: Mapped[str] = mapped_column(Text)
    before_payload: Mapped[str | None] = mapped_column(Text)
    after_payload: Mapped[str] = mapped_column(Text)
    payload_version: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[str] = mapped_column(String(200))


class TaskSnapshotRecord(Base):
    __tablename__ = "task_snapshots"
    __table_args__ = (
        UniqueConstraint("task_id", "ordinal"),
        CheckConstraint(
            "scope IN ('task','occurrence') AND payload_version=1", name="snapshot_scope_version"
        ),
        CheckConstraint(
            "length(trim(label)) BETWEEN 1 AND 200 AND ordinal>0 AND file_count BETWEEN 0 AND 100",
            name="snapshot_values",
        ),
        Index("ix_task_snapshots_time", "task_id", "captured_at", "id"),
        Index("ix_task_snapshots_scope_time", "task_id", "scope_key", "captured_at", "id"),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(String(16))
    scope_key: Mapped[str] = mapped_column(String(64))
    occurrence_start: Mapped[datetime | None] = mapped_column(UTCDateTime())
    label: Mapped[str] = mapped_column(String(200))
    ordinal: Mapped[int] = mapped_column(Integer)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime())
    committed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    job_id: Mapped[str] = mapped_column(String(32), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    payload_version: Mapped[int] = mapped_column(Integer, default=1)
    info_payload: Mapped[str] = mapped_column(Text)
    file_count: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(String(200))


class SnapshotFileRecord(Base):
    __tablename__ = "snapshot_files"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "position"),
        UniqueConstraint("snapshot_id", "source_kind", "source_id"),
        CheckConstraint(
            "position>=0 AND fixed_size_bytes>=0 AND length(fixed_checksum)=64 AND payload_version=1",
            name="snapshot_file_values",
        ),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("task_snapshots.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("file_assets.id", ondelete="RESTRICT"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(500))
    original_name: Mapped[str] = mapped_column(String(500))
    role: Mapped[str] = mapped_column(String(16))
    memo: Mapped[str] = mapped_column(Text)
    family_id: Mapped[str] = mapped_column(String(32))  # provenance, not a deletion cascade
    source_kind: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[int] = mapped_column(Integer)
    source_payload: Mapped[str] = mapped_column(Text)
    fixed_size_bytes: Mapped[int] = mapped_column(Integer)
    fixed_checksum: Mapped[str] = mapped_column(String(64))
    payload_version: Mapped[int] = mapped_column(Integer, default=1)
    asset: Mapped[FileAssetRecord] = relationship(lazy="joined")
    snapshot: Mapped[TaskSnapshotRecord] = relationship(lazy="select")


class TaskMutationReceiptRecord(Base):
    __tablename__ = "task_mutation_receipts"
    __table_args__ = (CheckConstraint("payload_version=1", name="mutation_payload_version"),)
    command_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    command_kind: Mapped[str] = mapped_column(String(64))
    committed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    payload_version: Mapped[int] = mapped_column(Integer, default=1)
    result_payload: Mapped[str] = mapped_column(Text)


class TaskOccurrenceRecord(Base):
    __tablename__ = "task_occurrences"
    __table_args__ = (
        UniqueConstraint("task_id", "occurrence_start"),
        Index("ix_task_occurrences_window", "task_id", "status", "occurrence_start"),
        CheckConstraint(
            "status IN ('pending','completed','skipped','canceled')",
            name="valid_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    occurrence_start: Mapped[datetime] = mapped_column(UTCDateTime())
    occurrence_end: Mapped[datetime | None] = mapped_column(UTCDateTime())
    effective_start: Mapped[datetime | None] = mapped_column(UTCDateTime())
    effective_end: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(16), default="pending")
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    result_note: Mapped[str] = mapped_column(Text, default="")


class ReminderRecord(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        CheckConstraint("relation IN ('start','end','absolute')", name="valid_relation"),
        Index("ix_reminders_next_fire", "enabled", "next_fire_at"),
        Index("ix_reminders_schedule_pending", "enabled", "schedule_initialized"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    relation: Mapped[str] = mapped_column(String(16))
    offset_minutes: Mapped[int | None] = mapped_column(Integer)
    absolute_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_fired_key: Mapped[str | None] = mapped_column(String(200))
    next_fire_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_occurrence_start: Mapped[datetime | None] = mapped_column(UTCDateTime())
    schedule_initialized: Mapped[bool] = mapped_column(Boolean, default=False)


class ReminderDeliveryRecord(Base):
    __tablename__ = "reminder_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('fired','snoozed','acknowledged','completed','deferred')",
            name="valid_status",
        ),
        Index("ix_reminder_deliveries_due", "status", "snoozed_until"),
        Index("ix_reminder_deliveries_schedule", "scheduled_at", "status"),
        Index("ix_reminder_deliveries_cleanup", "status", "acknowledged_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reminder_id: Mapped[int] = mapped_column(
        ForeignKey("reminders.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    occurrence_start: Mapped[datetime | None] = mapped_column(UTCDateTime())
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime())
    fire_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="fired")
    first_fired_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_fired_at: Mapped[datetime] = mapped_column(UTCDateTime())
    snoozed_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ChecklistItemRecord(Base):
    __tablename__ = "checklist_items"
    __table_args__ = (Index("ix_checklist_items_task_position", "task_id", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(String(500))
    is_done: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class FileAssetRecord(Base):
    __tablename__ = "file_assets"
    __table_args__ = (
        CheckConstraint("kind IN ('mutable','frozen')", name="asset_kind"),
        CheckConstraint("state IN ('active','pending_delete')", name="asset_state"),
        CheckConstraint(
            "(kind='mutable' AND fixed_size_bytes IS NULL AND fixed_checksum IS NULL AND frozen_at IS NULL) OR (kind='frozen' AND fixed_size_bytes IS NOT NULL AND fixed_size_bytes>=0 AND fixed_checksum IS NOT NULL AND length(fixed_checksum)=64 AND frozen_at IS NOT NULL)",
            name="asset_fixed_values",
        ),
        Index("ix_file_assets_observed", "observed_checksum"),
        Index("ix_file_assets_fixed", "fixed_checksum"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)
    kind: Mapped[str] = mapped_column(String(16), default="mutable")
    state: Mapped[str] = mapped_column(String(16), default="active")
    stored_name: Mapped[str] = mapped_column(String(255), unique=True)
    relative_path: Mapped[str] = mapped_column(String(1000), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(UTC))
    fixed_size_bytes: Mapped[int | None] = mapped_column(Integer)
    fixed_checksum: Mapped[str | None] = mapped_column(String(64))
    frozen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    observed_size_bytes: Mapped[int | None] = mapped_column(Integer)
    observed_checksum: Mapped[str | None] = mapped_column(String(64))
    observed_mtime_ns: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_check_status: Mapped[str | None] = mapped_column(String(32))
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    missing_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class DocumentFamilyRecord(Base):
    __tablename__ = "document_families"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(UTC))
    next_version_number: Mapped[int] = mapped_column(Integer, default=1)


class FileOperationReceiptRecord(Base):
    __tablename__ = "file_operation_receipts"
    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    operation_kind: Mapped[str] = mapped_column(String(32))
    committed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    payload_version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[str] = mapped_column(Text)


class AttachmentVersionRecord(Base):
    __tablename__ = "attachment_versions"
    __table_args__ = (
        Index("ix_versions_task_created", "task_id", "created_at", "id"),
        Index("ix_versions_family_created", "family_id", "created_at", "id"),
        CheckConstraint(
            "source_attachment_id IS NULL OR source_version_id IS NULL",
            name="version_single_source",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    family_id: Mapped[str] = mapped_column(
        ForeignKey("document_families.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("file_assets.id", ondelete="RESTRICT"), index=True
    )
    label: Mapped[str] = mapped_column(String(200))
    suggestion_number: Mapped[int] = mapped_column(Integer)
    original_name: Mapped[str] = mapped_column(String(500))
    memo: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    metadata_revision: Mapped[int] = mapped_column(Integer, default=0)
    source_attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachments.id", ondelete="SET NULL"), index=True
    )
    source_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachment_versions.id", ondelete="SET NULL"), index=True
    )
    source_payload: Mapped[str] = mapped_column(Text, default="{}")
    asset: Mapped[FileAssetRecord] = relationship(lazy="joined")


class AttachmentRecord(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        Index("ix_attachments_detached_created", "detached_at", "created_at"),
        Index("ix_attachments_created_id", "created_at", "id"),
        Index("ix_attachments_task_order", "task_id", "sort_order", "id"),
        Index("ix_attachments_family_created", "family_id", "created_at", "id"),
        CheckConstraint("role IN ('general','original','work')", name="attachment_role"),
        CheckConstraint(
            "source_attachment_id IS NULL OR source_version_id IS NULL",
            name="attachment_single_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    original_name: Mapped[str] = mapped_column(String(500))
    display_name: Mapped[str] = mapped_column(String(500))
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("file_assets.id", ondelete="RESTRICT"), unique=True
    )
    family_id: Mapped[str] = mapped_column(
        ForeignKey("document_families.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), default="general")
    memo: Mapped[str] = mapped_column(Text, default="")
    metadata_revision: Mapped[int] = mapped_column(Integer, default=0)
    source_attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachments.id", ondelete="SET NULL"), index=True
    )
    source_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachment_versions.id", ondelete="SET NULL"), index=True
    )
    source_payload: Mapped[str] = mapped_column(Text, default="{}")
    asset: Mapped[FileAssetRecord] = relationship(lazy="joined")
    family: Mapped[DocumentFamilyRecord] = relationship()
    size_bytes: Mapped[int] = mapped_column(Integer)
    checksum: Mapped[str | None] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    detached_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    def __init__(self, **kwargs: Any) -> None:
        # Compatibility for existing callers: physical metadata is owned only by
        # the asset, never duplicated in the attachment table.
        asset_values = {
            name: kwargs.pop(name)
            for name in (
                "stored_name",
                "relative_path",
                "observed_size_bytes",
                "observed_checksum",
                "observed_mtime_ns",
                "observed_at",
                "last_check_status",
                "last_checked_at",
                "missing_at",
            )
            if name in kwargs
        }
        if "asset" not in kwargs and "asset_id" not in kwargs:
            kwargs["asset"] = FileAssetRecord(
                **asset_values, created_at=kwargs.get("created_at", datetime.now(UTC))
            )
        if "family" not in kwargs and "family_id" not in kwargs:
            kwargs["family"] = DocumentFamilyRecord(
                task_id=kwargs["task_id"], created_at=kwargs.get("created_at", datetime.now(UTC))
            )
        kwargs.setdefault("display_name", kwargs["original_name"])
        super().__init__(**kwargs)

    @hybrid_property
    def stored_name(self) -> str:
        return self.asset.stored_name

    @stored_name.inplace.setter
    def _set_stored_name(self, value: str) -> None:
        self.asset.stored_name = value

    @stored_name.inplace.expression
    @classmethod
    def _stored_name_expression(cls) -> ColumnElement[str]:
        return (
            select(FileAssetRecord.stored_name)
            .where(FileAssetRecord.id == cls.asset_id)
            .correlate(cls)
            .scalar_subquery()
        )

    @hybrid_property
    def relative_path(self) -> str:
        return self.asset.relative_path

    @relative_path.inplace.setter
    def _set_relative_path(self, value: str) -> None:
        self.asset.relative_path = value

    @relative_path.inplace.expression
    @classmethod
    def _relative_path_expression(cls) -> ColumnElement[str]:
        return (
            select(FileAssetRecord.relative_path)
            .where(FileAssetRecord.id == cls.asset_id)
            .correlate(cls)
            .scalar_subquery()
        )

    @hybrid_property
    def observed_size_bytes(self) -> int | None:
        return self.asset.observed_size_bytes

    @observed_size_bytes.inplace.setter
    def _set_observed_size_bytes(self, value: int | None) -> None:
        self.asset.observed_size_bytes = value

    @observed_size_bytes.inplace.expression
    @classmethod
    def _observed_size_bytes_expression(cls) -> ColumnElement[int | None]:
        return (
            select(FileAssetRecord.observed_size_bytes)
            .where(FileAssetRecord.id == cls.asset_id)
            .correlate(cls)
            .scalar_subquery()
        )

    @hybrid_property
    def observed_checksum(self) -> str | None:
        return self.asset.observed_checksum

    @observed_checksum.inplace.setter
    def _set_observed_checksum(self, value: str | None) -> None:
        self.asset.observed_checksum = value

    @observed_checksum.inplace.expression
    @classmethod
    def _observed_checksum_expression(cls) -> ColumnElement[str | None]:
        return (
            select(FileAssetRecord.observed_checksum)
            .where(FileAssetRecord.id == cls.asset_id)
            .correlate(cls)
            .scalar_subquery()
        )

    @hybrid_property
    def observed_mtime_ns(self) -> int | None:
        return self.asset.observed_mtime_ns

    @observed_mtime_ns.inplace.setter
    def _set_observed_mtime_ns(self, value: int | None) -> None:
        self.asset.observed_mtime_ns = value

    @observed_mtime_ns.inplace.expression
    @classmethod
    def _observed_mtime_ns_expression(cls) -> ColumnElement[int | None]:
        return (
            select(FileAssetRecord.observed_mtime_ns)
            .where(FileAssetRecord.id == cls.asset_id)
            .correlate(cls)
            .scalar_subquery()
        )

    @hybrid_property
    def observed_at(self) -> datetime | None:
        return self.asset.observed_at

    @observed_at.inplace.setter
    def _set_observed_at(self, value: datetime | None) -> None:
        self.asset.observed_at = value

    @observed_at.inplace.expression
    @classmethod
    def _observed_at_expression(cls) -> ColumnElement[datetime | None]:
        return (
            select(FileAssetRecord.observed_at)
            .where(FileAssetRecord.id == cls.asset_id)
            .correlate(cls)
            .scalar_subquery()
        )

    @hybrid_property
    def last_check_status(self) -> str | None:
        return self.asset.last_check_status

    @last_check_status.inplace.setter
    def _set_last_check_status(self, value: str | None) -> None:
        self.asset.last_check_status = value

    @last_check_status.inplace.expression
    @classmethod
    def _last_check_status_expression(cls) -> ColumnElement[str | None]:
        return (
            select(FileAssetRecord.last_check_status)
            .where(FileAssetRecord.id == cls.asset_id)
            .correlate(cls)
            .scalar_subquery()
        )

    @hybrid_property
    def last_checked_at(self) -> datetime | None:
        return self.asset.last_checked_at

    @last_checked_at.inplace.setter
    def _set_last_checked_at(self, value: datetime | None) -> None:
        self.asset.last_checked_at = value

    @last_checked_at.inplace.expression
    @classmethod
    def _last_checked_at_expression(cls) -> ColumnElement[datetime | None]:
        return (
            select(FileAssetRecord.last_checked_at)
            .where(FileAssetRecord.id == cls.asset_id)
            .correlate(cls)
            .scalar_subquery()
        )

    @hybrid_property
    def missing_at(self) -> datetime | None:
        return self.asset.missing_at

    @missing_at.inplace.setter
    def _set_missing_at(self, value: datetime | None) -> None:
        self.asset.missing_at = value

    @missing_at.inplace.expression
    @classmethod
    def _missing_at_expression(cls) -> ColumnElement[datetime | None]:
        return (
            select(FileAssetRecord.missing_at)
            .where(FileAssetRecord.id == cls.asset_id)
            .correlate(cls)
            .scalar_subquery()
        )


class WorkLogRecord(Base):
    __tablename__ = "work_logs"
    __table_args__ = (
        Index("ix_work_logs_date_task", "log_date", "task_id"),
        Index("ix_work_logs_date_updated", "log_date", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    occurrence_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_occurrences.id", ondelete="SET NULL")
    )
    log_date: Mapped[date] = mapped_column(Date)
    content: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text, default="")
    priority_snapshot: Mapped[str] = mapped_column(String(16), default="normal")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class NoteRecord(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note_date: Mapped[date | None] = mapped_column(Date, index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class AppSettingRecord(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
