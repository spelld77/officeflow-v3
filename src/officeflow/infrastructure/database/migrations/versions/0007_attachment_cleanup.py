"""Track detached attachments until the user permanently deletes them."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0007_attachment_cleanup"
down_revision: str | None = "0006_attachment_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("attachments")}
    if "detached_at" not in columns:
        op.add_column("attachments", sa.Column("detached_at", sa.DateTime(), nullable=True))
    indexes = {item["name"] for item in inspect(bind).get_indexes("attachments")}
    if "ix_attachments_detached_created" not in indexes:
        op.create_index(
            "ix_attachments_detached_created",
            "attachments",
            ["detached_at", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("attachments")}
    if "ix_attachments_detached_created" in indexes:
        op.drop_index("ix_attachments_detached_created", table_name="attachments")
    columns = {item["name"] for item in inspect(bind).get_columns("attachments")}
    if "detached_at" in columns:
        op.drop_column("attachments", "detached_at")
