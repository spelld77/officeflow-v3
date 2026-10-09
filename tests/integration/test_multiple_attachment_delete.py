"""Selections share one safe delete transaction without widening its targets."""

from dataclasses import asdict
from uuid import uuid4

import pytest
from sqlalchemy import event

from officeflow.application.attachment_deletion import (
    DeleteCommand,
    DeleteTarget,
    decode_delete_target,
)
from officeflow.application.attachment_documents import DocumentOperation, fingerprint
from officeflow.application.attachments import AttachmentOperationError
from officeflow.infrastructure.database.models import AttachmentVersionRecord
from tests.integration.test_attachment_deletion import deleting
from tests.integration.test_attachment_documents import command, seed


def selection(task_id, targets):
    from officeflow.application.attachment_deletion import DeleteSelection

    return DeleteSelection(task_id, tuple(targets))


def pair(env):
    first = seed(env)
    source = env.database.parent / "other.txt"
    source.write_bytes(b"second")
    return first, env.attachments.attach(first.task_id, source)


def request_many(service, first, second):
    plan = service.plan(
        selection(
            first.task_id,
            [DeleteTarget("attachment", first.id), DeleteTarget("attachment", second.id)],
        )
    )
    return DeleteCommand(uuid4().hex, plan.target, plan.fingerprint)


def test_two_attachments_delete_together_and_replay_never_consumes_new_files(tmp_path):
    with deleting(tmp_path) as (env, _, service, _, storage):
        first, second = pair(env)
        paths = [env.attachments.path_for_open(item.id) for item in (first, second)]
        request = request_many(service, first, second)
        assert not service.execute(request).pending
        assert all(not path.exists() for path in paths)
        assert env.attachments.attachment_metadata_for_task(first.task_id) == ()
        source = env.database.parent / "replacement.txt"
        source.write_bytes(b"replacement")
        new = env.attachments.attach(first.task_id, source)
        assert service.execute(request).already_committed
        assert env.attachments.path_for_open(new.id).read_bytes() == b"replacement"
        assert not storage.has_pending()


@pytest.mark.parametrize(
    "case", ["lock", "changed", "cancel", "db", "other_task", "missing", "trash"]
)
def test_any_selection_failure_leaves_all_original_files(tmp_path, case):
    with deleting(tmp_path) as (env, _, service, _, storage):
        first, second = pair(env)
        targets = [DeleteTarget("attachment", first.id), DeleteTarget("attachment", second.id)]
        if case == "other_task":
            unrelated = seed(env, b"unrelated")
            targets[1] = DeleteTarget("attachment", unrelated.id)
        elif case == "missing":
            targets[1] = DeleteTarget("attachment", 999999)
        paths = [env.attachments.path_for_open(item.id) for item in (first, second)]
        lock = None

        def fail(conn, cursor, statement, parameters, context, many):
            if statement.startswith("INSERT INTO file_operation_receipts"):
                raise RuntimeError("injected multi-delete transaction failure")

        try:
            with pytest.raises(
                (ValueError, LookupError, AttachmentOperationError, OSError, RuntimeError)
            ):
                plan = service.plan(selection(first.task_id, targets))
                request = DeleteCommand(uuid4().hex, plan.target, plan.fingerprint)
                if case == "lock":
                    lock = paths[1].open("rb")
                elif case == "changed":
                    env.attachments.update_memo(second.id, "changed", expected_revision=0)
                elif case == "cancel":
                    storage._checkpoint = lambda stage: setattr(
                        storage, "_test_cancel", stage == "moved"
                    )
                elif case == "trash":
                    env.tasks.move_to_trash(first.task_id)
                elif case == "db":
                    event.listen(env.engine, "before_cursor_execute", fail)
                service.execute(
                    request, cancel_requested=lambda: getattr(storage, "_test_cancel", False)
                )
        finally:
            if lock:
                lock.close()
            if case == "db" and event.contains(env.engine, "before_cursor_execute", fail):
                event.remove(env.engine, "before_cursor_execute", fail)
        assert [path.read_bytes() for path in paths] == [b"initial", b"second"]
        assert len(env.attachments.attachment_metadata_for_task(first.task_id)) == 2


@pytest.mark.parametrize("include_shared_version", [False, True])
def test_shared_frozen_content_deleted_only_when_all_selected_refs_are_removed(
    tmp_path, include_shared_version
):
    with deleting(tmp_path) as (env, creation, service, _, _):
        source = seed(env)
        creation.execute(command(source, DocumentOperation.ORIGINAL))
        source = env.attachments.get_metadata(source.id)
        with env.sessions.transaction() as session:
            version = AttachmentVersionRecord(
                task_id=source.task_id,
                family_id=source.family_id,
                asset_id=source.asset_id,
                label="shared fixture",
                suggestion_number=1,
                original_name=source.name,
                memo="",
                created_at=source.created_at,
                metadata_revision=0,
            )
            session.add(version)
            session.flush()
            version_id = version.id
        second_path = env.database.parent / "other.txt"
        second_path.write_bytes(b"other")
        second = env.attachments.attach(source.task_id, second_path)
        targets = [DeleteTarget("attachment", source.id), DeleteTarget("attachment", second.id)]
        if include_shared_version:
            targets.append(DeleteTarget("version", version_id))
        plan = service.plan(selection(source.task_id, targets))
        assert plan.removable_count == (2 if include_shared_version else 1)
        assert plan.kept_asset_count == (0 if include_shared_version else 1)
        service.execute(DeleteCommand(uuid4().hex, plan.target, plan.fingerprint))
        frozen = env.attachments.storage_root / source.relative_path
        assert frozen.exists() is not include_shared_version
        with env.sessions.transaction() as session:
            assert (
                session.get(AttachmentVersionRecord, version_id) is None
            ) is include_shared_version


def test_legacy_target_fingerprint_and_duplicate_or_task_selection_guard():
    old = DeleteCommand("a" * 32, DeleteTarget("attachment", 1), "b" * 64)
    assert old.request_fingerprint == fingerprint(
        {
            "job_id": "a" * 32,
            "target": {"kind": "attachment", "id": 1},
            "plan_fingerprint": "b" * 64,
            "include_versions": False,
        }
    )
    assert asdict(old)["target"] == {"kind": "attachment", "id": 1}
    for targets in [[], [DeleteTarget("task", 1)], [DeleteTarget("attachment", 1)] * 2]:
        with pytest.raises(ValueError):
            selection(1, targets)
    valid = selection(1, [DeleteTarget("attachment", 1), DeleteTarget("version", 2)])
    assert decode_delete_target({**asdict(valid), "items": list(asdict(valid)["items"])}) == valid
    for raw in [
        {"kind": "selection", "id": 1, "items": [{"kind": "task", "id": 1}]},
        {"kind": "selection", "id": 1, "items": [{"kind": "selection", "id": 1, "items": []}]},
        {
            "kind": "selection",
            "id": 1,
            "items": [{"kind": "attachment", "id": n} for n in range(1, 102)],
        },
    ]:
        with pytest.raises(ValueError):
            decode_delete_target(raw)


def test_mixed_selection_removes_only_chosen_attachment_and_version(tmp_path):
    with deleting(tmp_path) as (env, creation, service, _, _):
        first, second = pair(env)
        first_version = creation.execute(
            command(first, DocumentOperation.VERSION, label="selected")
        )
        kept = creation.execute(command(first, DocumentOperation.VERSION, label="keep"))
        plan = service.plan(
            selection(
                first.task_id,
                [
                    DeleteTarget("attachment", first.id),
                    DeleteTarget("version", first_version.version_id),
                ],
            )
        )
        assert plan.attachment_count == plan.version_count == plan.kept_versions == 1
        result = service.execute(DeleteCommand(uuid4().hex, plan.target, plan.fingerprint))
        assert not result.pending
        assert env.attachments.get_metadata(second.id).name == second.name
        with env.sessions.transaction() as session:
            assert session.get(AttachmentVersionRecord, first_version.version_id) is None
            assert session.get(AttachmentVersionRecord, kept.version_id) is not None
