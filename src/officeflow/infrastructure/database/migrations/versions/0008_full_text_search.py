"""Add synchronized FTS5 indexes for task and work-log body search."""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_full_text_search"
down_revision: str | None = "0007_attachment_cleanup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS task_search USING fts5(
            title,
            description,
            result_note,
            tokenize = 'unicode61'
        )
        """
    )
    op.execute(
        """
        INSERT INTO task_search(rowid, title, description, result_note)
        SELECT id, title, description, result_note FROM tasks
        WHERE id NOT IN (SELECT rowid FROM task_search)
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS task_search_insert
        AFTER INSERT ON tasks BEGIN
            INSERT INTO task_search(rowid, title, description, result_note)
            VALUES (new.id, new.title, new.description, new.result_note);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS task_search_update
        AFTER UPDATE OF title, description, result_note ON tasks BEGIN
            DELETE FROM task_search WHERE rowid = old.id;
            INSERT INTO task_search(rowid, title, description, result_note)
            VALUES (new.id, new.title, new.description, new.result_note);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS task_search_delete
        AFTER DELETE ON tasks BEGIN
            DELETE FROM task_search WHERE rowid = old.id;
        END
        """
    )

    op.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS work_log_search USING fts5(
            task_id UNINDEXED,
            content,
            result,
            tokenize = 'unicode61'
        )
        """
    )
    op.execute(
        """
        INSERT INTO work_log_search(rowid, task_id, content, result)
        SELECT id, task_id, content, result FROM work_logs
        WHERE id NOT IN (SELECT rowid FROM work_log_search)
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS work_log_search_insert
        AFTER INSERT ON work_logs BEGIN
            INSERT INTO work_log_search(rowid, task_id, content, result)
            VALUES (new.id, new.task_id, new.content, new.result);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS work_log_search_update
        AFTER UPDATE OF task_id, content, result ON work_logs BEGIN
            DELETE FROM work_log_search WHERE rowid = old.id;
            INSERT INTO work_log_search(rowid, task_id, content, result)
            VALUES (new.id, new.task_id, new.content, new.result);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS work_log_search_delete
        AFTER DELETE ON work_logs BEGIN
            DELETE FROM work_log_search WHERE rowid = old.id;
        END
        """
    )


def downgrade() -> None:
    for trigger in (
        "work_log_search_delete",
        "work_log_search_update",
        "work_log_search_insert",
        "task_search_delete",
        "task_search_update",
        "task_search_insert",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute("DROP TABLE IF EXISTS work_log_search")
    op.execute("DROP TABLE IF EXISTS task_search")
