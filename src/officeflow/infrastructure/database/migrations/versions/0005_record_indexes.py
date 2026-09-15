"""Add indexes used by checklist ordering and work-log browsing."""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

revision: str = "0005_record_indexes"
down_revision: str | None = "0004_reminder_deliveries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    checklist_indexes = {item["name"] for item in inspector.get_indexes("checklist_items")}
    if "ix_checklist_items_task_position" not in checklist_indexes:
        op.create_index(
            "ix_checklist_items_task_position",
            "checklist_items",
            ["task_id", "position"],
        )
    work_log_indexes = {item["name"] for item in inspector.get_indexes("work_logs")}
    if "ix_work_logs_date_updated" not in work_log_indexes:
        op.create_index(
            "ix_work_logs_date_updated",
            "work_logs",
            ["log_date", "updated_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    checklist_indexes = {item["name"] for item in inspector.get_indexes("checklist_items")}
    if "ix_checklist_items_task_position" in checklist_indexes:
        op.drop_index("ix_checklist_items_task_position", table_name="checklist_items")
    work_log_indexes = {item["name"] for item in inspector.get_indexes("work_logs")}
    if "ix_work_logs_date_updated" in work_log_indexes:
        op.drop_index("ix_work_logs_date_updated", table_name="work_logs")
