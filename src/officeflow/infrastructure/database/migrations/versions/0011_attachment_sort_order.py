"""Preserve existing attachment order and enable manual ordering."""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

revision: str = "0011_attachment_sort_order"
down_revision: str | None = "0010_attachment_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    columns = {column["name"] for column in inspect(connection).get_columns("attachments")}
    if "sort_order" not in columns:
        op.execute("ALTER TABLE attachments ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
        op.execute("""WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY task_id ORDER BY created_at DESC, id DESC
            ) * 10 AS position FROM attachments
        ) UPDATE attachments SET sort_order = (
            SELECT position FROM ranked WHERE ranked.id = attachments.id
        )""")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_attachments_task_order ON attachments(task_id, sort_order, id)"
    )
    if connection.exec_driver_sql(
        "SELECT 1 FROM attachments WHERE sort_order IS NULL OR sort_order < 0 LIMIT 1"
    ).fetchone():
        raise ValueError("첨부 순서 변환을 검증하지 못했습니다.")


def downgrade() -> None:
    op.drop_index("ix_attachments_task_order", table_name="attachments")
    op.execute("ALTER TABLE attachments DROP COLUMN sort_order")
