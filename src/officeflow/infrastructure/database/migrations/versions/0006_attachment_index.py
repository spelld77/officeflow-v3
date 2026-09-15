"""Add the attachment availability lookup index."""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

revision: str = "0006_attachment_index"
down_revision: str | None = "0005_record_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    indexes = {item["name"] for item in inspect(bind).get_indexes("attachments")}
    if "ix_attachments_task_missing" not in indexes:
        op.create_index(
            "ix_attachments_task_missing",
            "attachments",
            ["task_id", "missing_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {item["name"] for item in inspect(bind).get_indexes("attachments")}
    if "ix_attachments_task_missing" in indexes:
        op.drop_index("ix_attachments_task_missing", table_name="attachments")
