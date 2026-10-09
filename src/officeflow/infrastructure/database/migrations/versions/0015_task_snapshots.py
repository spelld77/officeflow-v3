"""User-selected immutable task states and frozen file preservation references."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_task_snapshots"
down_revision: str | None = "0014_task_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("occurrence_start", sa.DateTime()),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("committed_at", sa.DateTime(), nullable=False),
        sa.Column("job_id", sa.String(32), nullable=False, unique=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("info_payload", sa.Text(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("summary", sa.String(200), nullable=False),
        sa.UniqueConstraint("task_id", "ordinal"),
        sa.CheckConstraint(
            "scope IN ('task','occurrence') AND payload_version=1", name="snapshot_scope_version"
        ),
        sa.CheckConstraint(
            "length(trim(label)) BETWEEN 1 AND 200 AND ordinal>0 AND file_count BETWEEN 0 AND 100",
            name="snapshot_values",
        ),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_task_snapshots_time", "task_snapshots", ["task_id", "captured_at", "id"])
    op.create_index(
        "ix_task_snapshots_scope_time",
        "task_snapshots",
        ["task_id", "scope_key", "captured_at", "id"],
    )
    op.create_table(
        "snapshot_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.Integer(),
            sa.ForeignKey("task_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            sa.String(32),
            sa.ForeignKey("file_assets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("original_name", sa.String(500), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("memo", sa.Text(), nullable=False),
        sa.Column("family_id", sa.String(32), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("source_payload", sa.Text(), nullable=False),
        sa.Column("fixed_size_bytes", sa.Integer(), nullable=False),
        sa.Column("fixed_checksum", sa.String(64), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("snapshot_id", "position"),
        sa.UniqueConstraint("snapshot_id", "source_kind", "source_id"),
        sa.CheckConstraint(
            "position>=0 AND fixed_size_bytes>=0 AND length(fixed_checksum)=64 AND payload_version=1",
            name="snapshot_file_values",
        ),
        sqlite_autoincrement=True,
    )
    for name in ("snapshot_id", "asset_id"):
        op.create_index("ix_snapshot_files_" + name, "snapshot_files", [name])
    op.execute("""CREATE TRIGGER snapshot_file_frozen BEFORE INSERT ON snapshot_files BEGIN
        SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM file_assets f WHERE f.id=NEW.asset_id AND f.kind='frozen' AND f.state='active' AND f.fixed_size_bytes=NEW.fixed_size_bytes AND f.fixed_checksum=NEW.fixed_checksum)
        THEN RAISE(ABORT,'snapshot requires verified frozen asset') END; END""")
    op.execute(
        "CREATE TRIGGER snapshot_immutable BEFORE UPDATE ON task_snapshots BEGIN SELECT RAISE(ABORT,'snapshot is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER snapshot_file_immutable BEFORE UPDATE ON snapshot_files BEGIN SELECT RAISE(ABORT,'snapshot file is immutable'); END"
    )


def downgrade() -> None:
    op.drop_table("snapshot_files")
    op.drop_table("task_snapshots")
