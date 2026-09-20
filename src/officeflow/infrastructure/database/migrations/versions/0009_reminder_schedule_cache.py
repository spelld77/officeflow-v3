"""Cache the next reminder schedule and index old delivery cleanup candidates."""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Boolean, Column, DateTime, inspect

revision: str = "0009_reminder_schedule_cache"
down_revision: str | None = "0008_full_text_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    reminder_columns = {
        column["name"] for column in inspect(bind).get_columns("reminders")
    }
    with op.batch_alter_table("reminders") as batch:
        if "next_fire_at" not in reminder_columns:
            batch.add_column(Column("next_fire_at", DateTime(), nullable=True))
        if "next_occurrence_start" not in reminder_columns:
            batch.add_column(Column("next_occurrence_start", DateTime(), nullable=True))
        if "schedule_initialized" not in reminder_columns:
            batch.add_column(
                Column("schedule_initialized", Boolean(), nullable=False, server_default="0")
            )
    op.create_index(
        "ix_reminders_schedule_pending",
        "reminders",
        ["enabled", "schedule_initialized"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_reminders_next_fire",
        "reminders",
        ["enabled", "next_fire_at"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_reminder_deliveries_cleanup",
        "reminder_deliveries",
        ["status", "acknowledged_at"],
        unique=False,
        if_not_exists=True,
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS reminder_schedule_task_changed
        AFTER UPDATE OF starts_at, ends_at, timezone, recurrence_rule, status, deleted_at
        ON tasks BEGIN
            UPDATE reminders
            SET next_fire_at = NULL,
                next_occurrence_start = NULL,
                schedule_initialized = 0
            WHERE task_id = new.id;
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS reminder_schedule_task_changed")
    op.drop_index(
        "ix_reminder_deliveries_cleanup",
        table_name="reminder_deliveries",
        if_exists=True,
    )
    op.drop_index(
        "ix_reminders_schedule_pending",
        table_name="reminders",
        if_exists=True,
    )
    op.drop_index("ix_reminders_next_fire", table_name="reminders", if_exists=True)
    with op.batch_alter_table("reminders") as batch:
        batch.drop_column("schedule_initialized")
        batch.drop_column("next_occurrence_start")
        batch.drop_column("next_fire_at")
