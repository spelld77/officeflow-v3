"""Create the OfficeFlow v3 baseline schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from officeflow.infrastructure.database import models  # noqa: F401
from officeflow.infrastructure.database.base import Base

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # The attachment baseline must not follow today's normalized ORM schema:
    # historical revisions 0006-0012 operate on these physical columns.
    op.create_table(
        "attachments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("original_name", sa.String(500), nullable=False),
        sa.Column("stored_name", sa.String(255), nullable=False, unique=True),
        sa.Column("relative_path", sa.String(1000), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("missing_at", sa.DateTime()),
    )
    excluded = {
        "attachments",
        "file_assets",
        "document_families",
        "file_operation_receipts",
        "attachment_versions",
        "task_history",
        "task_mutation_receipts",
        "task_snapshots",
        "snapshot_files",
        "task_folder_links",
    }
    Base.metadata.create_all(
        bind=bind,
        tables=[table for table in Base.metadata.tables.values() if table.name not in excluded],
    )
    op.create_index("ix_attachments_task_id", "attachments", ["task_id"])
    op.create_index("ix_attachments_created_id", "attachments", ["created_at", "id"])


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
