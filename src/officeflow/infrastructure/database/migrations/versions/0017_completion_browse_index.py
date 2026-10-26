"""Index-only completion browsing. No row or file changes."""

from collections.abc import Sequence

from alembic import op

revision: str = "0017_completion_browse_index"
down_revision: str | None = "0016_task_folder_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_task_occurrences_completed",
        "task_occurrences",
        ["status", "completed_at", "task_id", "id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_task_occurrences_completed", table_name="task_occurrences")
