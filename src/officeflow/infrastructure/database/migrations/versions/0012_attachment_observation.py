"""Keep original metadata; add independent, nullable current observations."""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

revision: str = "0012_attachment_observation"
down_revision: str | None = "0011_attachment_sort_order"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = {"observed_size_bytes": "INTEGER", "observed_checksum": "VARCHAR(64)", "observed_mtime_ns": "INTEGER", "observed_at": "DATETIME", "last_check_status": "VARCHAR(32)", "last_checked_at": "DATETIME"}


def upgrade() -> None:
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("attachments")}
    for name, sql_type in _COLUMNS.items():
        if name not in existing:
            op.execute(f"ALTER TABLE attachments ADD COLUMN {name} {sql_type}")
    op.execute("CREATE INDEX IF NOT EXISTS ix_attachments_task_observed ON attachments(task_id, observed_checksum)")


def downgrade() -> None:
    op.drop_index("ix_attachments_task_observed", table_name="attachments")
    for name in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE attachments DROP COLUMN {name}")
