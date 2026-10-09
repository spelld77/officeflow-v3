"""Normalize file ownership without moving or reading existing attachment files."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_attachment_assets"
down_revision: str | None = "0012_attachment_observation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OBSERVATION = (
    "observed_size_bytes",
    "observed_checksum",
    "observed_mtime_ns",
    "observed_at",
    "last_check_status",
    "last_checked_at",
    "missing_at",
)


def _create_asset_tables() -> None:
    # A historical revision must not import tomorrow's ORM table definitions.
    op.create_table(
        "file_assets",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("stored_name", sa.String(255), nullable=False, unique=True),
        sa.Column("relative_path", sa.String(1000), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("fixed_size_bytes", sa.Integer()),
        sa.Column("fixed_checksum", sa.String(64)),
        sa.Column("frozen_at", sa.DateTime()),
        sa.Column("observed_size_bytes", sa.Integer()),
        sa.Column("observed_checksum", sa.String(64)),
        sa.Column("observed_mtime_ns", sa.Integer()),
        sa.Column("observed_at", sa.DateTime()),
        sa.Column("last_check_status", sa.String(32)),
        sa.Column("last_checked_at", sa.DateTime()),
        sa.Column("missing_at", sa.DateTime()),
        sa.CheckConstraint("kind IN ('mutable','frozen')", name="asset_kind"),
        sa.CheckConstraint("state IN ('active','pending_delete')", name="asset_state"),
        sa.CheckConstraint(
            "(kind='mutable' AND fixed_size_bytes IS NULL AND fixed_checksum IS NULL AND frozen_at IS NULL) OR (kind='frozen' AND fixed_size_bytes IS NOT NULL AND fixed_size_bytes>=0 AND fixed_checksum IS NOT NULL AND length(fixed_checksum)=64 AND frozen_at IS NOT NULL)",
            name="asset_fixed_values",
        ),
    )
    op.create_index("ix_file_assets_observed", "file_assets", ["observed_checksum"])
    op.create_index("ix_file_assets_fixed", "file_assets", ["fixed_checksum"])
    op.create_table(
        "document_families",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("next_version_number", sa.Integer(), nullable=False),
    )
    op.create_index("ix_document_families_task_id", "document_families", ["task_id"])
    op.create_table(
        "file_operation_receipts",
        sa.Column("job_id", sa.String(32), primary_key=True),
        sa.Column("operation_kind", sa.String(32), nullable=False),
        sa.Column("committed_at", sa.DateTime(), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )


def _create_version_table() -> None:
    op.create_table(
        "attachment_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "family_id",
            sa.String(32),
            sa.ForeignKey("document_families.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            sa.String(32),
            sa.ForeignKey("file_assets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("suggestion_number", sa.Integer(), nullable=False),
        sa.Column("original_name", sa.String(500), nullable=False),
        sa.Column("memo", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("metadata_revision", sa.Integer(), nullable=False),
        sa.Column(
            "source_attachment_id",
            sa.Integer(),
            sa.ForeignKey("attachments.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "source_version_id",
            sa.Integer(),
            sa.ForeignKey("attachment_versions.id", ondelete="SET NULL"),
        ),
        sa.Column("source_payload", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "source_attachment_id IS NULL OR source_version_id IS NULL",
            name="version_single_source",
        ),
    )
    for column in ("task_id", "family_id", "asset_id", "source_attachment_id", "source_version_id"):
        op.create_index(f"ix_attachment_versions_{column}", "attachment_versions", [column])
    op.create_index(
        "ix_versions_task_created", "attachment_versions", ["task_id", "created_at", "id"]
    )
    op.create_index(
        "ix_versions_family_created", "attachment_versions", ["family_id", "created_at", "id"]
    )


def _replace_attachment_table() -> None:
    # Alembic's generic SQLite batch table would point its new self-FK at the
    # old table. DROP then probes all new rows for every old row (quadratic).
    # Point the staging table at itself; SQLite retargets that FK on rename.
    target = "_phase2_attachments"
    op.create_table(
        target,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("original_name", sa.String(500), nullable=False),
        sa.Column("display_name", sa.String(500), nullable=False),
        sa.Column(
            "asset_id",
            sa.String(32),
            sa.ForeignKey("file_assets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "family_id",
            sa.String(32),
            sa.ForeignKey("document_families.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False, server_default="general"),
        sa.Column("memo", sa.Text(), nullable=False, server_default=""),
        sa.Column("metadata_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "source_attachment_id", sa.Integer(), sa.ForeignKey(f"{target}.id", ondelete="SET NULL")
        ),
        sa.Column(
            "source_version_id",
            sa.Integer(),
            sa.ForeignKey("attachment_versions.id", ondelete="SET NULL"),
        ),
        sa.Column("source_payload", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(64)),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("detached_at", sa.DateTime()),
        sa.UniqueConstraint("asset_id", name="uq_attachments_asset"),
        sa.CheckConstraint(
            "role IN ('general','original','work')", name=op.f("ck_attachments_attachment_role")
        ),
        sa.CheckConstraint(
            "source_attachment_id IS NULL OR source_version_id IS NULL",
            name=op.f("ck_attachments_attachment_single_source"),
        ),
    )
    columns = "id,task_id,original_name,display_name,asset_id,family_id,size_bytes,checksum,sort_order,created_at,detached_at"
    op.execute(f"INSERT INTO {target} ({columns}) SELECT {columns} FROM attachments")
    op.drop_table("attachments")
    op.rename_table(target, "attachments")
    for name, columns in (
        ("ix_attachments_task_id", ["task_id"]),
        ("ix_attachments_detached_created", ["detached_at", "created_at"]),
        ("ix_attachments_created_id", ["created_at", "id"]),
        ("ix_attachments_task_order", ["task_id", "sort_order", "id"]),
        ("ix_attachments_family_id", ["family_id"]),
        ("ix_attachments_family_created", ["family_id", "created_at", "id"]),
        ("ix_attachments_source_attachment_id", ["source_attachment_id"]),
        ("ix_attachments_source_version_id", ["source_version_id"]),
    ):
        op.create_index(name, "attachments", columns)


def upgrade() -> None:
    connection = op.get_bind()
    if connection.exec_driver_sql(
        "SELECT 1 FROM attachments GROUP BY relative_path HAVING count(*)>1 LIMIT 1"
    ).fetchone():
        raise ValueError("첨부 경로가 중복되어 이행하지 않았습니다. 보호 사본을 확인하세요.")
    for name in ("insert", "update", "delete"):
        op.execute(f"DROP TRIGGER IF EXISTS attachment_search_{name}")
    op.execute("DROP TABLE IF EXISTS attachment_search")
    _create_asset_tables()
    op.execute(
        "CREATE TEMP TABLE attachment_asset_map AS SELECT id, lower(hex(randomblob(16))) AS asset_id, lower(hex(randomblob(16))) AS family_id FROM attachments"
    )
    op.execute("CREATE UNIQUE INDEX attachment_asset_map_id ON attachment_asset_map(id)")
    observation_columns = ",".join(_OBSERVATION)
    op.execute(
        f"INSERT INTO file_assets(id,kind,state,stored_name,relative_path,created_at,{observation_columns}) SELECT m.asset_id,'mutable','active',a.stored_name,a.relative_path,a.created_at,{','.join('a.' + n for n in _OBSERVATION)} FROM attachments a JOIN attachment_asset_map m ON m.id=a.id"
    )
    op.execute(
        "INSERT INTO document_families(id,task_id,created_at,next_version_number) SELECT m.family_id,a.task_id,a.created_at,1 FROM attachments a JOIN attachment_asset_map m ON m.id=a.id"
    )
    for name, sql_type in (
        ("asset_id", "VARCHAR(32)"),
        ("family_id", "VARCHAR(32)"),
        ("display_name", "VARCHAR(500)"),
    ):
        op.execute(f"ALTER TABLE attachments ADD COLUMN {name} {sql_type}")
    op.execute(
        "UPDATE attachments SET asset_id=(SELECT asset_id FROM attachment_asset_map WHERE id=attachments.id), family_id=(SELECT family_id FROM attachment_asset_map WHERE id=attachments.id), display_name=original_name"
    )
    # Source-version FK is created before any non-null reference can exist.
    _create_version_table()
    _replace_attachment_table()
    op.execute("DROP TABLE attachment_asset_map")
    op.execute(
        "CREATE VIRTUAL TABLE attachment_search USING fts5(display_name, tokenize='trigram')"
    )
    op.execute(
        "INSERT INTO attachment_search(rowid,display_name) SELECT id,display_name FROM attachments"
    )
    op.execute(
        "CREATE TRIGGER attachment_search_insert AFTER INSERT ON attachments BEGIN INSERT INTO attachment_search(rowid,display_name) VALUES(new.id,new.display_name); END"
    )
    op.execute(
        "CREATE TRIGGER attachment_search_update AFTER UPDATE OF display_name ON attachments BEGIN DELETE FROM attachment_search WHERE rowid=old.id; INSERT INTO attachment_search(rowid,display_name) VALUES(new.id,new.display_name); END"
    )
    op.execute(
        "CREATE TRIGGER attachment_search_delete AFTER DELETE ON attachments BEGIN DELETE FROM attachment_search WHERE rowid=old.id; END"
    )
    for event in ("INSERT", "UPDATE"):
        op.execute(f"""CREATE TRIGGER attachment_asset_guard_{event.lower()} BEFORE {event} ON attachments
            BEGIN SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM file_assets f JOIN document_families d ON d.id=new.family_id
                WHERE f.id=new.asset_id AND f.state='active' AND d.task_id=new.task_id
                AND ((new.role='original' AND f.kind='frozen') OR (new.role IN ('general','work') AND f.kind='mutable')))
                THEN RAISE(ABORT,'invalid attachment asset/family') END; END""")
        op.execute(f"""CREATE TRIGGER version_asset_guard_{event.lower()} BEFORE {event} ON attachment_versions
            BEGIN SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM file_assets f JOIN document_families d ON d.id=new.family_id
                WHERE f.id=new.asset_id AND f.state='active' AND f.kind='frozen' AND d.task_id=new.task_id)
                THEN RAISE(ABORT,'invalid version asset/family') END; END""")
    op.execute("""CREATE TRIGGER file_asset_immutable BEFORE UPDATE OF kind,relative_path,stored_name,fixed_size_bytes,fixed_checksum,frozen_at ON file_assets
        WHEN old.kind IS NOT new.kind OR old.relative_path IS NOT new.relative_path OR old.stored_name IS NOT new.stored_name
            OR old.fixed_size_bytes IS NOT new.fixed_size_bytes OR old.fixed_checksum IS NOT new.fixed_checksum OR old.frozen_at IS NOT new.frozen_at
        BEGIN SELECT RAISE(ABORT,'file asset identity is immutable'); END""")
    op.execute("""CREATE TRIGGER attachment_initial_name_immutable BEFORE UPDATE OF original_name ON attachments
        WHEN old.original_name IS NOT new.original_name
        BEGIN SELECT RAISE(ABORT,'initial attachment name is immutable'); END""")
    op.execute("""CREATE TRIGGER version_capture_immutable BEFORE UPDATE OF asset_id,family_id,task_id,original_name,created_at,suggestion_number,source_payload ON attachment_versions
        WHEN old.asset_id IS NOT new.asset_id OR old.family_id IS NOT new.family_id OR old.task_id IS NOT new.task_id
            OR old.original_name IS NOT new.original_name OR old.created_at IS NOT new.created_at
            OR old.suggestion_number IS NOT new.suggestion_number OR old.source_payload IS NOT new.source_payload
        BEGIN SELECT RAISE(ABORT,'version capture is immutable'); END""")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchone():
        raise ValueError("첨부 자산 이행의 연결을 확인하지 못했습니다.")


def downgrade() -> None:
    raise RuntimeError(
        "보관 파일을 버리는 다운그레이드는 지원하지 않습니다. 이전 전체 백업을 별도 루트에 복원하세요."
    )
