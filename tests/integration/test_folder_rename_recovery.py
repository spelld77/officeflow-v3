import json
import sqlite3
import time
from dataclasses import replace

import pytest
from alembic import command

from officeflow.application.folder_links import FolderLinkService, FolderRelinkCandidate
from officeflow.infrastructure.database.folder_link_repository import SqlAlchemyFolderLinkRepository
from officeflow.infrastructure.database.migrate import migration_config, upgrade_database
from officeflow.infrastructure.folder_identity import inspect, probe
from tests.integration.test_attachment_export import files  # noqa: F401


def service(files):  # noqa: F811
    return FolderLinkService(
        SqlAlchemyFolderLinkRepository(files[3]._sessions), (files[5].data_root,)
    )


@pytest.mark.parametrize("custom", [False, True])
def test_real_ntfs_rename_requires_confirmation_and_keeps_source(files, tmp_path, custom):  # noqa: F811
    links = service(files)
    old = tmp_path / "01.폴더_a"
    old.mkdir()
    (old / "자료.txt").write_text("unchanged")
    link = links.save(files[1].id, "별도 이름" if custom else old.name, old)
    assert link.folder_identity
    new = old.with_name("04.폴더_a")
    old.rename(new)
    candidate = links.inspect_open(link)
    assert isinstance(candidate, FolderRelinkCandidate) and candidate.path == new
    assert links.list(link.task_id)[0] == link
    assert links.confirm_candidate(candidate) == new
    saved = links.list(link.task_id)[0]
    assert saved.name == ("별도 이름" if custom else new.name)
    assert saved.folder_identity == link.folder_identity
    assert links.inspect_open(saved) == new
    assert (new / "자료.txt").read_text() == "unchanged"


def test_existing_links_are_not_enrolled_by_open_or_display_rename(files, tmp_path):  # noqa: F811
    links = service(files)
    old = tmp_path / "old"
    old.mkdir()
    link = links.repository.save(files[1].id, old.name, str(old), None)
    assert link.folder_identity is None
    assert links.inspect_open(link) == old
    renamed = links.rename(link.task_id, link, "display")
    assert renamed.folder_identity is None
    old.rename(tmp_path / "new")
    with pytest.raises(ValueError, match="연결은 유지"):
        links.inspect_open(renamed)
    enrolled = links.save(
        link.task_id, renamed.name, tmp_path / "new", identity=link.id, expected=renamed
    )
    assert enrolled.folder_identity


@pytest.mark.parametrize(
    "change", ["same_old_name", "candidate_moved", "concurrent_edit", "other_pc"]
)
def test_changed_or_stale_target_is_never_silently_saved(files, tmp_path, change):  # noqa: F811
    links = service(files)
    old = tmp_path / "old"
    old.mkdir()
    link = links.save(files[1].id, old.name, old)
    new = tmp_path / "new"
    old.rename(new)
    candidate = links.inspect_open(link)
    assert isinstance(candidate, FolderRelinkCandidate)
    if change == "same_old_name":
        old.mkdir()

        def operation():
            return links.inspect_open(link)
    elif change == "candidate_moved":
        new.rename(tmp_path / "third")

        def operation():
            return links.confirm_candidate(candidate)
    elif change == "concurrent_edit":
        links.rename(link.task_id, link, "another window")

        def operation():
            return links.confirm_candidate(candidate)
    else:
        token = json.loads(link.folder_identity)
        token["machine"] = "another-pc"

        def operation():
            return links.inspect_open(replace(link, folder_identity=json.dumps(token)))

    with pytest.raises(ValueError):
        operation()
    assert links.list(link.task_id)[0].path == link.path


def test_migration_adds_only_null_identity_for_existing_links(files, tmp_path):  # noqa: F811
    links = service(files)
    folder = tmp_path / "legacy"
    folder.mkdir()
    link = links.repository.save(files[1].id, "Legacy", str(folder), None)
    database = files[5].data_root / "officeflow.db"
    command.downgrade(migration_config(database), "0017_completion_browse_index")
    with sqlite3.connect(database) as db:
        before = db.execute("SELECT * FROM task_folder_links").fetchall()
    upgrade_database(database)
    with sqlite3.connect(database) as db:
        after = db.execute("SELECT * FROM task_folder_links").fetchall()
        assert [row[:-1] for row in after] == before
        assert after[0][-1] is None
    assert links.list(link.task_id)[0] == link
    assert list((database.parent / "schema-backups").glob("*.db"))


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
def test_probe_helper_is_bounded_and_reaped(tmp_path, mode):
    count = [0]

    def canceled():
        count[0] += 1
        return mode == "cancel" and count[0] >= 2

    started = time.monotonic()
    with pytest.raises(ValueError):
        probe(tmp_path, (), timeout=0 if mode == "timeout" else 5, canceled=canceled)
    assert time.monotonic() - started < 2


def test_permission_error_does_not_start_candidate_search(tmp_path, monkeypatch):
    import officeflow.infrastructure.folder_identity as module

    monkeypatch.setattr(
        module, "_validate", lambda *_: (_ for _ in ()).throw(PermissionError("offline"))
    )
    monkeypatch.setattr(module, "directory_identity", lambda *_: pytest.fail("unexpected search"))
    with pytest.raises(PermissionError):
        inspect({"path": str(tmp_path), "roots": [], "expected": "tracked", "search": True})


def test_null_identity_probe_does_not_collect_folder_id(tmp_path, monkeypatch):
    import officeflow.infrastructure.folder_identity as module

    monkeypatch.setattr(
        module, "directory_identity", lambda *_: pytest.fail("retroactive collection")
    )
    assert inspect({"path": str(tmp_path), "roots": []})["identity"] is None


def test_0018_backup_missing_identity_column_is_rejected(files):  # noqa: F811
    from officeflow.infrastructure.backup import BackupError, _check_restore_schema

    database = files[5].data_root / "officeflow.db"
    with sqlite3.connect(database) as db:
        db.execute("ALTER TABLE task_folder_links DROP COLUMN folder_identity")
    with pytest.raises(BackupError, match="폴더 연결"):
        _check_restore_schema(database)


@pytest.mark.parametrize("mode", ["unsupported", "ambiguous", "limit"])
def test_unsupported_or_unbounded_search_never_selects_by_name(tmp_path, monkeypatch, mode):
    import officeflow.infrastructure.folder_identity as module

    token = json.dumps({"v": 1, "machine": "local", "volume": 7})
    monkeypatch.setattr(module, "machine_key", lambda: "local")
    if mode == "unsupported":
        monkeypatch.setattr(module, "directory_identity", lambda *_: None)
        assert inspect({"path": str(tmp_path), "roots": [], "capture": True})["identity"] is None
        return
    (tmp_path / "01.name").mkdir()
    (tmp_path / "04.name").mkdir()
    monkeypatch.setattr(module, "directory_identity", lambda *_: token)
    if mode == "limit":
        monkeypatch.setattr(module, "SCAN_LIMIT", 0)
    with pytest.raises(ValueError, match="연결은 유지"):
        inspect({"path": str(tmp_path / "missing"), "roots": [], "expected": token, "search": True})
