"""Add indexes for recurrence templates and occurrence history."""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

revision: str = "0003_recurrence_indexes"
down_revision: str | None = "0002_task_query_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_tasks_recurrence_window", "tasks", ("recurrence_until", "starts_at")),
    (
        "ix_task_occurrences_window",
        "task_occurrences",
        ("task_id", "status", "occurrence_start"),
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    for name, table, columns in INDEXES:
        existing = {index["name"] for index in inspect(bind).get_indexes(table)}
        if name not in existing:
            op.create_index(name, table, list(columns), unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    for name, table, _columns in reversed(INDEXES):
        existing = {index["name"] for index in inspect(bind).get_indexes(table)}
        if name in existing:
            op.drop_index(name, table_name=table)
