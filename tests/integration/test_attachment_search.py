from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, event, text

from officeflow.application.attachment_search import (
    AttachmentKind,
    AttachmentSearchInterrupted,
    AttachmentSearchQuery,
    AttachmentSearchService,
)
from officeflow.application.attachments import AttachmentService
from officeflow.application.records import RecordService
from officeflow.application.tasks import TaskDraft, TaskQuery, TaskService, TaskView
from officeflow.domain.attachment import Attachment
from officeflow.domain.enums import TaskStatus
from officeflow.infrastructure.attachments.storage import ManagedAttachmentStorage
from officeflow.infrastructure.database.attachment_repository import SqlAlchemyAttachmentRepository
from officeflow.infrastructure.database.attachment_search_repository import (
    SqlAlchemyAttachmentSearchRepository,
)
from officeflow.infrastructure.database.migrate import (
    migration_config,
    needs_attachment_search_upgrade,
    upgrade_database,
)
from officeflow.infrastructure.database.record_repository import SqlAlchemyRecordRepository
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository

NOW = datetime(2026, 10, 1, 3, tzinfo=UTC)


def test_failed_index_rolls_back_and_retry_succeeds(tmp_path: Path) -> None:
    database = tmp_path / "old.db"
    upgrade_database(database)
    command.downgrade(migration_config(database), "0009_reminder_schedule_cache")

    def fail(_connection, _cursor, statement, _parameters, _context, _many):
        if "CREATE TRIGGER IF NOT EXISTS attachment_search_update" in statement:
            raise sqlite3.OperationalError("synthetic index failure")

    event.listen(Engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(sqlite3.OperationalError, match="synthetic"):
            upgrade_database(database)
    finally:
        event.remove(Engine, "before_cursor_execute", fail)
    with closing(sqlite3.connect(database)) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0009_reminder_schedule_cache"
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='attachment_search'"
            ).fetchone()
            is None
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert needs_attachment_search_upgrade(database)
    upgrade_database(database)
    assert not needs_attachment_search_upgrade(database)


def test_insufficient_space_leaves_old_database_unchanged(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from officeflow.infrastructure.database import migrate

    database = tmp_path / "old.db"
    upgrade_database(database)
    command.downgrade(migration_config(database), "0009_reminder_schedule_cache")
    monkeypatch.setattr(migrate.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    with pytest.raises(OSError, match="공간"):
        upgrade_database(database)
    assert needs_attachment_search_upgrade(database)
    assert not (tmp_path / "schema-backups").exists()


@pytest.mark.parametrize("old_schema", [True, False])
def test_backup_restore_recreates_search_and_preserves_bytes(
    tmp_path: Path, old_schema: bool
) -> None:
    from officeflow.bootstrap.paths import AppPaths
    from officeflow.infrastructure.backup import BackupManager

    root = tmp_path / "data"
    root.mkdir()
    paths = AppPaths(root)
    paths.ensure_directories()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        task = env.tasks.create(TaskDraft(title="복원 업무"))
        source = tmp_path / "복원보고서.txt"
        source.write_bytes(b"preserved content")
        attached = env.attachments.attach(task.id, source)
        stored_path = env.attachments.path_for_open(attached.id_required)
    if old_schema:
        command.downgrade(migration_config(paths.database_file), "0009_reminder_schedule_cache")
    manager = BackupManager(paths)
    backup = manager.create_backup()
    stored_path.write_bytes(b"changed content")
    manager.stage_restore(backup.path)
    manager.apply_pending_restore()
    upgrade_database(paths.database_file)
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        page = env.search.search_page(AttachmentSearchQuery(search="복원보고서"))
        assert len(page.items) == 1
        assert (
            env.attachments.path_for_open(page.items[0].attachment_id).read_bytes()
            == b"preserved content"
        )


@dataclass
class SearchEnvironment:
    engine: Engine
    database: Path
    sessions: SessionFactory
    tasks: TaskService
    records: RecordService
    attachments: AttachmentService
    repository: SqlAlchemyAttachmentRepository
    search: AttachmentSearchService

    def add(self, task_id: int, name: str, *, created: datetime = NOW) -> Attachment:
        token = uuid4().hex
        return self.repository.add_attachment(
            Attachment(
                id=None,
                task_id=task_id,
                original_name=name,
                stored_name=token,
                relative_path=f"ab/cd/{token}",
                size_bytes=42,
                checksum=None,
                created_at=created,
            )
        )


@contextmanager
def search_environment(
    root: Path, *, attachment_root: Path | None = None
) -> Iterator[SearchEnvironment]:
    database = root / "officeflow.db"
    upgrade_database(database)
    engine = create_database_engine(database)
    sessions = SessionFactory(engine)
    tasks = TaskService(SqlAlchemyTaskRepository(sessions))
    repository = SqlAlchemyAttachmentRepository(sessions)
    env = SearchEnvironment(
        engine,
        database,
        sessions,
        tasks,
        RecordService(SqlAlchemyRecordRepository(sessions), tasks),
        AttachmentService(
            repository, ManagedAttachmentStorage(attachment_root or root / "attachments"), tasks
        ),
        repository,
        AttachmentSearchService(SqlAlchemyAttachmentSearchRepository(sessions)),
    )
    try:
        yield env
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "term",
    [
        "보고서",
        "보고",
        "최종",
        "XLSX",
        "9월업무",
        "보고 최종",
        "50%",
        "_최종",
        'a"b',
        '"""',
        "--",
        "'OR",
        "%' OR 1=1",
        "NOT",
        "!!!",
    ],
)
def test_literal_substrings_and_fts_special_characters(tmp_path: Path, term: str) -> None:
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="업무"))
        item = env.add(task.id, '9월업무보고서_최종_50%_a"b_"""_--_\'OR_%\' OR 1=1_NOT_!!!.xlsx')
        page = env.search.search_page(AttachmentSearchQuery(search=term))
        assert [hit.attachment_id for hit in page.items] == [item.id]
        assert env.tasks.query(TaskQuery(view=TaskView.ALL, search=term)).total == 1
        assert (
            env.tasks.query(TaskQuery(view=TaskView.ALL, search=term))
            .items[0]
            .matched_attachment_name
            == item.original_name
        )


def test_words_must_match_same_file_and_task_results_are_unique(tmp_path: Path) -> None:
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="구매"))
        env.add(task.id, "견적.xlsx")
        env.add(task.id, "최종.pdf")
        assert not env.search.search_page(AttachmentSearchQuery(search="견적 최종")).items
        assert env.tasks.query(TaskQuery(search="견적 최종")).total == 0
        env.add(task.id, "최종견적.xlsx")
        env.add(task.id, "최종견적.pdf")
        page = env.tasks.query(TaskQuery(search="견적 최종", limit=1))
        assert page.total == 1 and len(page.items) == 1
        assert page.items[0].matched_attachment_count == 2


def test_state_options_missing_and_file_kinds(tmp_path: Path) -> None:
    with search_environment(tmp_path) as env:
        complete = env.tasks.create(TaskDraft(title="지난 업무", status=TaskStatus.COMPLETED))
        archived = env.tasks.create(TaskDraft(title="보관", status=TaskStatus.ARCHIVED))
        trash = env.tasks.create(TaskDraft(title="삭제"))
        a = env.add(complete.id, "보고서.xlsx")
        b = env.add(archived.id, "보고서.PDF")
        c = env.add(trash.id, "보고서.docx")
        d = env.add(trash.id, "보고서.png")
        env.repository.set_missing_at(a.id_required, NOW)
        env.attachments.unlink(d.id_required)
        env.tasks.move_to_trash(trash.id)
        base = env.search.search_page(AttachmentSearchQuery(search="보고"))
        assert {hit.attachment_id for hit in base.items} == {a.id, b.id}
        assert any(hit.missing for hit in base.items)
        with_trash = env.search.search_page(
            AttachmentSearchQuery(search="보고", include_trash=True)
        )
        assert {hit.attachment_id for hit in with_trash.items} == {a.id, b.id, c.id}
        assert (
            len(
                env.search.search_page(
                    AttachmentSearchQuery(search="보고", include_trash=True, include_detached=True)
                ).items
            )
            == 4
        )
        assert [
            hit.attachment_id
            for hit in env.search.search_page(AttachmentSearchQuery(kind=AttachmentKind.PDF)).items
        ] == [b.id]
        env.tasks.restore_from_trash(trash.id)
        env.attachments.restore(d.id_required)
        assert len(env.search.search_page(AttachmentSearchQuery()).items) == 4
        env.attachments.delete_file(d.id_required)
        assert len(env.search.search_page(AttachmentSearchQuery()).items) == 3


def test_cursor_pages_ties_and_attachment_date_boundaries(tmp_path: Path) -> None:
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="동일 이름"))
        ids = [env.add(task.id, "견적.xlsx").id for _ in range(7)]
        env.add(task.id, "견적.pdf", created=NOW - timedelta(days=1))
        query = AttachmentSearchQuery(
            search="견적", attached_after=NOW, attached_before=NOW + timedelta(days=1), limit=3
        )
        p1 = env.search.search_page(query)
        from dataclasses import replace

        p2 = env.search.search_page(replace(query, cursor=p1.next_cursor))
        p3 = env.search.search_page(replace(query, cursor=p2.next_cursor))
        assert [hit.attachment_id for hit in (*p1.items, *p2.items, *p3.items)] == list(
            reversed(ids)
        )
        assert p1.has_more and p2.has_more and not p3.has_more


def test_calendar_work_log_and_completion_scope(tmp_path: Path) -> None:
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="계약 검토", starts_at=NOW))
        env.add(task.id, "장비견적서.xlsx")
        env.records.add_work_log(task_id=task.id, log_date=date(2026, 10, 1), content="검토 기록")
        assert env.records.work_log_page(search="견적").total == 1
        assert (
            len(env.tasks.calendar_schedule(date(2026, 10, 1), date(2026, 10, 2), search="견적"))
            == 1
        )
        assert not env.tasks.calendar_schedule(date(2026, 9, 1), date(2026, 9, 2), search="견적")
        assert env.tasks.completed_search_page(search="견적").total == 0
        env.tasks.transition(task.id, TaskStatus.COMPLETED, now=NOW)
        assert env.tasks.completed_search_page(search="견적").total == 1
        assert (
            env.tasks.completed_search_page(
                search="견적", date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
            ).total
            == 0
        )


def test_old_database_backfill_backup_rename_and_delete_triggers(tmp_path: Path) -> None:
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="기존 데이터"))
        item = env.add(task.id, "구매견적.xlsx")
        command.downgrade(migration_config(env.database), "0009_reminder_schedule_cache")
        upgrade_database(env.database)
        copies = list((tmp_path / "schema-backups").glob("*.db"))
        assert len(copies) == 1
        with sqlite3.connect(copies[0]) as connection:
            assert (
                connection.execute("SELECT original_name FROM attachments").fetchone()[0]
                == "구매견적.xlsx"
            )
            assert (
                connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
                == "0009_reminder_schedule_cache"
            )
        assert env.search.search_page(AttachmentSearchQuery(search="견적")).items
        with env.engine.begin() as connection:
            connection.execute(
                text("UPDATE attachments SET original_name='새보고서.pdf' WHERE id=:id"),
                {"id": item.id},
            )
        assert not env.search.search_page(AttachmentSearchQuery(search="견적")).items
        assert env.search.search_page(AttachmentSearchQuery(search="보고서")).items
        env.tasks.move_to_trash(task.id)
        env.tasks.delete_permanently(task.id)
        with env.engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM attachment_search")) == 0
        upgrade_database(env.database)
        assert len(list((tmp_path / "schema-backups").glob("*.db"))) == 1


def test_cancel_timeout_and_short_input_are_not_empty_success(tmp_path: Path) -> None:
    with search_environment(tmp_path) as env:
        with pytest.raises(AttachmentSearchInterrupted):
            env.search.search_page(AttachmentSearchQuery(), cancel_requested=lambda: True)
        with pytest.raises(AttachmentSearchInterrupted):
            SqlAlchemyAttachmentSearchRepository(env.sessions, timeout_seconds=-1).search_page(
                AttachmentSearchQuery()
            )
        with pytest.raises(ValueError, match="2글자"):
            AttachmentSearchQuery(search="견")
        assert env.tasks.query(TaskQuery(search="견")).total == 0


def test_cancellation_during_sql_clears_progress_handler(tmp_path: Path) -> None:
    from sqlalchemy import insert

    from officeflow.infrastructure.database.models import AttachmentRecord
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="취소 검증"))
        with env.sessions.transaction() as session:
            session.execute(insert(AttachmentRecord), [
                dict(task_id=task.id, original_name=f"업무보고서{i}.pdf", stored_name=f"fake{i}",
                     relative_path=f"fake/{i}", size_bytes=1, created_at=NOW)
                for i in range(5000)
            ])
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            return calls >= 3

        with pytest.raises(AttachmentSearchInterrupted):
            env.search.search_page(AttachmentSearchQuery(search="없음"), cancel_requested=cancel)
        assert calls >= 3
        assert len(env.search.search_page(AttachmentSearchQuery(search="보고서")).items) == 50
