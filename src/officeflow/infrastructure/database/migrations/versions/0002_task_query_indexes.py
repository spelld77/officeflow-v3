"""Add indexes for grouped and paged task queries."""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

revision: str = "0002_task_query_indexes"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ix_tasks_active_schedule", ("deleted_at", "status", "ends_at", "starts_at")),
    ("ix_tasks_completed_at", ("deleted_at", "status", "completed_at")),
    ("ix_tasks_deleted_updated", ("deleted_at", "updated_at")),
)


def upgrade() -> None:
    existing = {index["name"] for index in inspect(op.get_bind()).get_indexes("tasks")}
    for name, columns in INDEXES:
        if name not in existing:
            op.create_index(name, "tasks", list(columns), unique=False)


def downgrade() -> None:
    existing = {index["name"] for index in inspect(op.get_bind()).get_indexes("tasks")}
    for name, _columns in reversed(INDEXES):
        if name in existing:
            op.drop_index(name, table_name="tasks")
