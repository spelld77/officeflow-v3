import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import Engine, event

from officeflow.application.attachment_search import AttachmentSearchQuery
from officeflow.application.tasks import TaskDraft
from officeflow.domain.attachment import AttachmentMove
from officeflow.infrastructure.database.migrate import (
    migration_config,
    needs_database_upgrade,
    upgrade_database,
)
from tests.integration.test_attachment_search import search_environment


def _seed_old(tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="기존 순서"))
        other = env.tasks.create(TaskDraft(title="다른 업무"))
        now = datetime(2026, 10, 6, tzinfo=UTC)
        a = env.add(task.id, "처음.txt", created=now)
        b = env.add(task.id, "중간.txt", created=now + timedelta(minutes=1))
        c = env.add(task.id, "최근.txt", created=now + timedelta(minutes=1))
        env.attachments.unlink(b.id)
        d = env.add(other.id, "다른.txt", created=now)
        database = env.database
    command.downgrade(migration_config(database), "0010_attachment_search")
    return database, task.id, (a.id, b.id, c.id, d.id)


def _rows(database):
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute("SELECT * FROM attachments ORDER BY id").fetchall()


def test_upgrade_preserves_existing_display_ids_metadata_search_and_detached_slot(tmp_path):
    database, task_id, (a, b, c, d) = _seed_old(tmp_path)
    before = _rows(database)
    assert needs_database_upgrade(database)
    upgrade_database(database)
    after = _rows(database)
    assert [row[:-1] for row in after] == before
    with closing(sqlite3.connect(database)) as connection:
        assert dict(connection.execute("SELECT id,sort_order FROM attachments")) == {a: 30, b: 20, c: 10, d: 10}
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    copies = list((tmp_path / "schema-backups").glob("pre-attachment-order-*.db"))
    assert len(copies) == 1
    assert _rows(copies[0]) == before
    with search_environment(tmp_path) as env:
        assert [item.id for item in env.repository.list_attachments(task_id)] == [c, a]
        assert env.search.search_page(AttachmentSearchQuery(search="최근")).items
        env.attachments.restore(b)
        assert [item.id for item in env.repository.list_attachments(task_id)] == [c, b, a]
    assert not needs_database_upgrade(database)
    upgrade_database(database)
    assert len(list((tmp_path / "schema-backups").glob("*.db"))) == 1


def test_failed_upgrade_rolls_back_column_and_revision_then_can_retry(tmp_path):
    database, _task_id, _ids = _seed_old(tmp_path)
    before = _rows(database)

    def fail(_connection, _cursor, statement, _parameters, _context, _many):
        if "CREATE INDEX IF NOT EXISTS ix_attachments_task_order" in statement:
            raise sqlite3.OperationalError("synthetic order migration failure")

    event.listen(Engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(sqlite3.OperationalError, match="synthetic"):
            upgrade_database(database)
    finally:
        event.remove(Engine, "before_cursor_execute", fail)
    assert _rows(database) == before
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0010_attachment_search"
        assert "sort_order" not in {row[1] for row in connection.execute("PRAGMA table_info(attachments)")}
    assert list((tmp_path / "schema-backups").glob("*.db"))
    upgrade_database(database)
    assert not needs_database_upgrade(database)


def test_low_space_and_backup_failure_never_start_upgrade(tmp_path, monkeypatch):
    database, _task_id, _ids = _seed_old(tmp_path)
    before = _rows(database)
    monkeypatch.setattr("officeflow.infrastructure.database.migrate.shutil.disk_usage", lambda _p: SimpleNamespace(free=0))
    with pytest.raises(OSError, match="공간"):
        upgrade_database(database)
    assert _rows(database) == before
    assert not (tmp_path / "schema-backups").exists()
    monkeypatch.setattr("officeflow.infrastructure.database.migrate.shutil.disk_usage", lambda _p: SimpleNamespace(free=10**12))
    (tmp_path / "schema-backups").write_text("cannot create a backup directory")
    with pytest.raises(FileExistsError):
        upgrade_database(database)
    assert _rows(database) == before


def test_unknown_revision_is_rejected_without_backup_or_schema_mutation(tmp_path):
    database, _task_id, _ids = _seed_old(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE alembic_version SET version_num='future_revision'")
        connection.commit()
    before = database.read_bytes()
    with pytest.raises(ValueError, match="지원하지 않는"):
        upgrade_database(database)
    assert database.read_bytes() == before
    assert not (tmp_path / "schema-backups").exists()


@pytest.mark.parametrize("movement,expected", [
    (AttachmentMove.UP, [1, 0, 2]), (AttachmentMove.DOWN, [0, 2, 1]),
    (AttachmentMove.FIRST, [1, 0, 2]), (AttachmentMove.LAST, [0, 2, 1]),
])
def test_moves_only_metadata_and_survives_reopen(tmp_path, monkeypatch, movement, expected):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="이동 업무"))
        items = [env.add(task.id, f"첨부{i}.txt") for i in range(3)]
        before = {item.id: item for item in items}
        updated_before = env.tasks.get(task.id).updated_at

        def no_io(*_args, **_kwargs):
            raise AssertionError("Order operations must not inspect or modify files")

        for method in ("exists", "checksum", "resolve", "import_file", "remove"):
            monkeypatch.setattr(env.attachments._storage, method, no_io)
        moved = env.attachments.move_attachment(task.id, items[1].id, movement)
        assert [item.id for item in moved] == [items[i].id for i in expected]
        for item in moved:
            assert replace(item, sort_order=before[item.id].sort_order) == before[item.id]
        assert env.tasks.get(task.id).updated_at == updated_before
        assert env.attachments.attachment_metadata_for_task(task.id) == moved
    with search_environment(tmp_path) as reopened:
        assert [item.id for item in reopened.repository.list_attachments(task.id)] == [items[i].id for i in expected]


def test_latest_state_boundaries_other_task_trash_and_detached_guards(tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="이동"))
        other = env.tasks.create(TaskDraft(title="다른 업무"))
        a, b, c = [env.add(task.id, f"파일{i}.txt") for i in range(3)]
        env.attachments.move_attachment(task.id, a.id, AttachmentMove.UP)
        assert [x.id for x in env.repository.list_attachments(task.id)] == [a.id, b.id, c.id]
        env.attachments.move_attachment(task.id, c.id, AttachmentMove.FIRST)
        # The request refers to ID + direction, not the old row index of b.
        env.attachments.move_attachment(task.id, b.id, AttachmentMove.UP)
        assert [x.id for x in env.repository.list_attachments(task.id)] == [c.id, b.id, a.id]
        with pytest.raises(LookupError):
            env.repository.move_attachment(other.id, b.id, AttachmentMove.FIRST)
        env.attachments.unlink(b.id)
        with pytest.raises(LookupError):
            env.repository.move_attachment(task.id, b.id, AttachmentMove.FIRST)
        env.tasks.move_to_trash(task.id)
        with pytest.raises(LookupError):
            env.repository.move_attachment(task.id, a.id, AttachmentMove.LAST)
        with pytest.raises(LookupError):
            env.attachments.move_attachment(task.id, a.id, AttachmentMove.LAST)


def test_detached_slot_is_reserved_and_restore_preserves_order_value(tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="복원"))
        a, b, c = [env.add(task.id, f"파일{i}.txt") for i in range(3)]
        env.attachments.unlink(b.id)
        env.attachments.move_attachment(task.id, c.id, AttachmentMove.FIRST)
        assert env.repository.get_attachment(b.id).sort_order == b.sort_order
        d = env.add(task.id, "새 파일.txt")
        assert d.sort_order > max(a.sort_order, b.sort_order, c.sort_order)
        env.attachments.restore(b.id)
        assert [x.id for x in env.repository.list_attachments(task.id)] == [c.id, b.id, a.id, d.id]


def test_concurrent_append_allocates_distinct_slots(tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="동시 등록"))
        with ThreadPoolExecutor(max_workers=4) as workers:
            items = list(workers.map(lambda i: env.add(task.id, f"파일{i}.txt"), range(12)))
        assert len({item.sort_order for item in items}) == 12
        assert [item.sort_order for item in env.repository.list_attachments(task.id)] == list(range(10, 121, 10))


def test_observer_failure_does_not_undo_commit_and_unsubscribe_is_safe(tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="구독"))
        a, b = [env.add(task.id, f"파일{i}.txt") for i in range(2)]
        seen = []
        unsubscribe = env.attachments.subscribe(seen.append)
        bad = env.attachments.subscribe(lambda _task_id: (_ for _ in ()).throw(RuntimeError("closed observer")))
        env.attachments.move_attachment(task.id, b.id, AttachmentMove.FIRST)
        assert seen == [task.id]
        assert env.repository.list_attachments(task.id)[0].id == b.id
        unsubscribe()
        unsubscribe()
        bad()
        env.attachments.move_attachment(task.id, a.id, AttachmentMove.FIRST)
        assert seen == [task.id]


def test_failed_move_rolls_back_all_positions(tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="원자적 이동"))
        items = [env.add(task.id, f"파일{i}.txt") for i in range(4)]
        before = env.repository.list_attachments(task.id)

        def fail(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.startswith("UPDATE attachments SET sort_order"):
                raise sqlite3.OperationalError("synthetic move failure")

        event.listen(Engine, "before_cursor_execute", fail)
        try:
            with pytest.raises(Exception, match="synthetic"):
                env.attachments.move_attachment(task.id, items[-1].id, AttachmentMove.FIRST)
        finally:
            event.remove(Engine, "before_cursor_execute", fail)
        assert env.repository.list_attachments(task.id) == before


@pytest.mark.parametrize("old_schema", [False, True])
def test_backup_restore_preserves_custom_order_or_upgrades_old_order(tmp_path, old_schema):
    from officeflow.bootstrap.paths import AppPaths
    from officeflow.infrastructure.backup import BackupManager

    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        task = env.tasks.create(TaskDraft(title="백업 순서"))
        items = []
        for i in range(3):
            source = tmp_path / f"실제파일{i}.txt"
            source.write_text(str(i))
            items.append(env.attachments.attach(task.id, source))
        env.attachments.move_attachment(task.id, items[1].id, AttachmentMove.FIRST)
    if old_schema:
        command.downgrade(migration_config(paths.database_file), "0010_attachment_search")
    manager = BackupManager(paths)
    saved = manager.create_backup()
    manager.stage_restore(saved.path)
    manager.apply_pending_restore()
    upgrade_database(paths.database_file)
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        expected = [items[i].id for i in ([2, 1, 0] if old_schema else [1, 0, 2])]
        assert [x.id for x in env.repository.list_attachments(task.id)] == expected
        for item in items:
            assert env.attachments.path_for_open(item.id).read_text() == str(items.index(item))
