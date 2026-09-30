"""Index original attachment filenames for literal substring search."""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_attachment_search"
down_revision: str | None = "0009_reminder_schedule_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql("SAVEPOINT attachment_search_upgrade")
    try:
        _create_index()
    except Exception:
        connection.exec_driver_sql("ROLLBACK TO attachment_search_upgrade")
        connection.exec_driver_sql("RELEASE attachment_search_upgrade")
        raise
    connection.exec_driver_sql("RELEASE attachment_search_upgrade")


def _create_index() -> None:
    op.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS attachment_search USING fts5(original_name, tokenize='trigram')"
    )
    op.execute("DELETE FROM attachment_search")
    op.execute(
        "INSERT INTO attachment_search(rowid, original_name) SELECT id, original_name FROM attachments"
    )
    op.execute("""CREATE TRIGGER IF NOT EXISTS attachment_search_insert AFTER INSERT ON attachments BEGIN
        INSERT INTO attachment_search(rowid, original_name) VALUES (new.id, new.original_name); END""")
    op.execute("""CREATE TRIGGER IF NOT EXISTS attachment_search_update AFTER UPDATE OF original_name ON attachments BEGIN
        DELETE FROM attachment_search WHERE rowid = old.id;
        INSERT INTO attachment_search(rowid, original_name) VALUES (new.id, new.original_name); END""")
    op.execute("""CREATE TRIGGER IF NOT EXISTS attachment_search_delete AFTER DELETE ON attachments BEGIN
        DELETE FROM attachment_search WHERE rowid = old.id; END""")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_attachments_created_id ON attachments(created_at, id)"
    )


def downgrade() -> None:
    op.drop_index("ix_attachments_created_id", table_name="attachments")
    for name in ("insert", "update", "delete"):
        op.execute(f"DROP TRIGGER IF EXISTS attachment_search_{name}")
    op.execute("DROP TABLE IF EXISTS attachment_search")
