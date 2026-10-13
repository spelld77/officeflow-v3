"""Direct provenance is read-only and respects each historical payload shape."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import event

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.task_snapshots import SnapshotCommand, SnapshotSelection
from officeflow.infrastructure.database.models import AttachmentRecord, FileAssetRecord
from tests.integration.test_document_lifecycle import lifecycle, original
from tests.integration.test_task_snapshots import service as snapshot_service


def work_from(facade, item, name="work.txt"):
    return facade.create(
        facade.catalog.get("attachment", item.id), DocumentOperation.WORK, filename=name
    ).attachment_id


def test_live_renamed_deleted_and_missing_source_information(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        work_id = work_from(facade, item)
        result = facade.catalog.source_summary(item.task_id, work_id)
        assert result.state == "live" and result.current_name == item.name
        renamed = env.attachments.rename_attachment(
            item.id, "renamed.txt", expected_revision=item.metadata_revision
        )
        result = facade.catalog.source_summary(item.task_id, work_id)
        assert result.current_name == renamed.name and result.name_at_creation == item.name
        env.attachments.delete_file(item.id)
        result = facade.catalog.source_summary(item.task_id, work_id)
        assert result.state == "deleted" and item.name in result.text
        assert env.attachments.get_metadata(work_id).name == "work.txt"


def test_version_and_snapshot_file_sources_are_not_assumed_to_be_originals(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        work_id = work_from(facade, item)
        version = facade.create(
            facade.catalog.get("attachment", work_id), DocumentOperation.VERSION, label="review"
        )
        from_version = facade.create(
            facade.catalog.get("version", version.version_id),
            DocumentOperation.WORK,
            filename="from-version.txt",
        )
        assert (
            "보관본" in facade.catalog.source_summary(item.task_id, from_version.attachment_id).text
        )
        snapshots = snapshot_service(env)
        snapshot = snapshots.create(
            SnapshotCommand(
                uuid4().hex,
                item.task_id,
                "before-send",
                (SnapshotSelection("attachment", item.id),),
                documents_closed=True,
            )
        )
        entry = facade.catalog.snapshot_files(item.task_id, snapshot.snapshot_id).items[0]
        from_snapshot = facade.create(entry, DocumentOperation.WORK, filename="from-snapshot.txt")
        summary = facade.catalog.source_summary(item.task_id, from_snapshot.attachment_id)
        assert (
            summary.state == "live"
            and "시점 보관" in summary.text
            and "before-send" in summary.text
        )


def test_preserved_modified_cache_is_valid_after_temporary_copy_is_removed(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        path = facade.prepare_open("attachment", item.id)
        path.write_bytes(b"saved edit")
        copy = facade.cache.by_path(path)
        copy = facade.cache.confirm_closed(copy, copy.use_token)
        work = facade.cache.preserve(copy, "saved-work.txt", facade.documents, lambda: False)
        assert not path.exists()
        result = facade.catalog.source_summary(item.task_id, work.attachment_id)
        assert result.kind == "cache" and result.state == "live"
        assert "수정 보기 사본" in result.text and "삭제" not in result.text


@pytest.mark.parametrize(
    "payload",
    [
        "broken",
        "[]",
        '{"version":1,"kind":"bad"}',
        '{"version":1,"kind":"attachment","id":999}',
        '"text"',
        '{"version":true,"kind":"attachment","id":1}',
        '{"version":1,"kind":"attachment","id":true}',
        '{"version":1,"kind":"attachment","id":1.0}',
        json.dumps({"version": 1, "kind": "attachment", "id": 1, "name": "x" * 16_385}),
    ],
)
def test_invalid_payload_is_unknown_not_file_damage(tmp_path, payload):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        work_id = work_from(facade, item)
        with env.sessions.transaction() as session:
            session.get(AttachmentRecord, work_id).source_payload = payload
        result = facade.catalog.source_summary(item.task_id, work_id)
        assert result.state == "unavailable" and result.text == "출처 확인 불가"
        assert env.attachments.get_metadata(work_id).name == "work.txt"


def test_cross_task_link_and_inactive_asset_do_not_leak_or_claim_deletion(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        work_id = work_from(facade, item)
        with env.sessions.transaction() as session:
            row = session.get(AttachmentRecord, work_id)
            payload = json.loads(row.source_payload)
            payload["kind"] = "snapshot_file"
            payload["task_id"] = item.task_id + 1
            row.source_payload = json.dumps(payload)
        assert facade.catalog.source_summary(item.task_id, work_id).state == "unavailable"
        with pytest.raises(LookupError):
            facade.catalog.source_summary(item.task_id + 1, work_id)
        with env.sessions.transaction() as session:
            row = session.get(AttachmentRecord, work_id)
            row.source_payload = "{}"
            session.get(FileAssetRecord, item.asset_id).state = "pending_delete"
        assert facade.catalog.source_summary(item.task_id, work_id).state == "unavailable"


def test_source_lookup_uses_no_write_statements(tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        work_id = work_from(facade, item)
        statements = []
        engine = env.sessions._bind
        # The returned values remain immutable and have no persistence side effects.
        before = env.attachments.get_metadata(work_id)
        result = facade.catalog.source_summary(item.task_id, work_id)
        assert result.state == "live" and env.attachments.get_metadata(work_id) == before

        def record(_c, _cur, statement, _p, _ctx, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            facade.catalog.source_summary(item.task_id, work_id)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert statements and not any(
            s.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for s in statements
        )
