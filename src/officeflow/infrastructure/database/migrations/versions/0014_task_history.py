"""Append-only semantic task history; no retroactive data or file changes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_task_history"
down_revision: str | None = "0013_attachment_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("occurrence_start", sa.DateTime()),
        sa.Column("command_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("changed_fields", sa.Text(), nullable=False),
        sa.Column("before_payload", sa.Text()),
        sa.Column("after_payload", sa.Text(), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("summary", sa.String(200), nullable=False),
        sa.UniqueConstraint("task_id", "command_id", "scope_key", "event_type"),
        sa.CheckConstraint("scope IN ('task','occurrence')", name="history_scope"),
        sa.CheckConstraint("event_type IN ('created','baseline','change')", name="history_event"),
        sa.CheckConstraint("payload_version=1", name="history_payload_version"),
        sa.CheckConstraint(
            "(scope='task' AND scope_key='task' AND occurrence_start IS NULL) OR (scope='occurrence' AND scope_key LIKE 'occ:%' AND occurrence_start IS NOT NULL)",
            name="history_scope_values",
        ),
        sqlite_autoincrement=True,
    )
    for name, columns in (
        ("ix_task_history_time", ["task_id", "recorded_at", "id"]),
        ("ix_task_history_scope_time", ["task_id", "scope_key", "recorded_at", "id"]),
        ("ix_task_history_scope_id", ["task_id", "scope_key", "id"]),
    ):
        op.create_index(name, "task_history", columns)
    op.create_index(
        "ux_task_history_initial",
        "task_history",
        ["task_id", "scope_key"],
        unique=True,
        sqlite_where=sa.text("event_type IN ('created','baseline')"),
    )
    op.create_table(
        "task_mutation_receipts",
        sa.Column("command_id", sa.String(64), primary_key=True),
        sa.Column(
            "task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("command_kind", sa.String(64), nullable=False),
        sa.Column("committed_at", sa.DateTime(), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("result_payload", sa.Text(), nullable=False),
        sa.CheckConstraint("payload_version=1", name="mutation_payload_version"),
    )
    op.create_index("ix_task_mutation_receipts_task_id", "task_mutation_receipts", ["task_id"])


def downgrade() -> None:
    op.drop_table("task_mutation_receipts")
    op.drop_table("task_history")
