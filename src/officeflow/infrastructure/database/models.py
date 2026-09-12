from __future__ import annotations

from datetime import date, datetime

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
)
from sqlalchemy.orm import Mapped, mapped_column

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
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
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


class TaskOccurrenceRecord(Base):
    __tablename__ = "task_occurrences"
    __table_args__ = (
        UniqueConstraint("task_id", "occurrence_start"),
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
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    relation: Mapped[str] = mapped_column(String(16))
    offset_minutes: Mapped[int | None] = mapped_column(Integer)
    absolute_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_fired_key: Mapped[str | None] = mapped_column(String(200))


class ChecklistItemRecord(Base):
    __tablename__ = "checklist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(String(500))
    is_done: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AttachmentRecord(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    original_name: Mapped[str] = mapped_column(String(500))
    stored_name: Mapped[str] = mapped_column(String(255), unique=True)
    relative_path: Mapped[str] = mapped_column(String(1000))
    size_bytes: Mapped[int] = mapped_column(Integer)
    checksum: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    missing_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class WorkLogRecord(Base):
    __tablename__ = "work_logs"
    __table_args__ = (Index("ix_work_logs_date_task", "log_date", "task_id"),)

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
