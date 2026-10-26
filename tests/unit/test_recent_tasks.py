import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from officeflow.application.recent_tasks import RecentTaskRef, RecentTasks, RecentTaskSummary
from officeflow.infrastructure.recent_tasks import RecentTaskStore

NOW = datetime(2026, 10, 26, tzinfo=UTC)


class Repository:
    def __init__(self):
        self.values = {}

    def summaries(self, ids):
        return {i: self.values[i] for i in ids if i in self.values}


def ref(i):
    return RecentTaskRef(i, NOW, None, NOW)


def test_mru_bounded_unique_task_and_clock_backwards():
    service = RecentTasks(Repository())
    for i in range(1, 22):
        service.record_viewed(ref(i))
    assert len(service.entries) == 20
    assert [r.task_id for r in service.entries] == list(range(21, 1, -1))
    service.record_viewed(replace(ref(2), viewed_at=NOW - timedelta(days=2), occurrence_start=NOW))
    assert service.entries[0].task_id == 2
    assert service.entries[0].occurrence_start == NOW
    assert len(service.entries) == 20


def test_resolve_uses_current_title_and_created_identity():
    repo = Repository()
    repo.values = {
        1: RecentTaskSummary(1, "changed", "completed", NOW, True),
        2: RecentTaskSummary(2, "reused", "active", NOW + timedelta(seconds=1), False),
    }
    resolved = RecentTasks(repo).resolve((ref(1), ref(2), ref(3)))
    assert len(resolved) == 1
    assert resolved[0][0].title == "changed"


def test_store_roundtrip_privacy_revision_clear_and_identity(tmp_path):
    database = tmp_path / "db"
    database.write_bytes(b"synthetic")
    path = tmp_path / "recent-tasks.json"
    store = RecentTaskStore(path, database)
    revision = store.reserve()
    store.save((ref(1), ref(2)), revision)
    assert store.load() == (ref(1), ref(2))
    payload = json.loads(path.read_text())
    assert set(payload["entries"][0]) == {
        "task_id",
        "task_created_at",
        "occurrence_start",
        "viewed_at",
    }
    newer = store.reserve()
    store.save((), newer)
    store.save((ref(1),), revision)
    assert store.load() == ()
    store.save((ref(1),), store.reserve())
    replacement = tmp_path / "newdb"
    replacement.write_bytes(b"different")
    replacement.replace(database)
    assert RecentTaskStore(path, database).load() == ()
    assert store.invalidate()
    assert not path.exists()


def test_store_bad_size_json_types_timezone_and_duplicate(tmp_path):
    database = tmp_path / "db"
    database.write_bytes(b"synthetic")
    path = tmp_path / "recent-tasks.json"
    store = RecentTaskStore(path, database)
    for value in (b"x" * 32769, b"{", b"[]", b"null"):
        path.write_bytes(value)
        assert store.load() == ()
    store.save((ref(1), ref(1)), store.reserve())
    assert store.load() == (ref(1),)
    value = json.loads(path.read_text())
    for row in value["entries"]:
        row["task_created_at"] = "2026-10-26"
    path.write_text(json.dumps(value))
    assert store.load() == ()
