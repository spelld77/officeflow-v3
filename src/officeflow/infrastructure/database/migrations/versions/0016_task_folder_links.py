"""External folder metadata only; never move or copy folder contents."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_task_folder_links"
down_revision: str | None = "0015_task_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_folder_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("length(trim(name)) BETWEEN 1 AND 200", name="folder_link_name"),
    )
    op.create_index(
        "ix_task_folder_links_order", "task_folder_links", ["task_id", "position", "id"]
    )


def downgrade() -> None:
    op.drop_table("task_folder_links")
