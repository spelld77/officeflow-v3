import json
import os
import time

import pytest

from officeflow.application.attachment_export import AttachmentExportService, FileReference
from officeflow.application.attachments import AttachmentCanceledError, AttachmentOperationError
from officeflow.infrastructure.attachments.drag_copies import DragCopyStore
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
from tests.integration.test_attachment_export import attach, files  # noqa: F401
from tests.integration.test_document_lifecycle import lifecycle, original


def test_multiple_copies_collision_and_independent_bytes(files, tmp_path):  # noqa: F811
    items = [
        attach(files, tmp_path, name=name, content=str(i).encode())
        for i, name in enumerate(("보고서.txt", "보고서.txt", "보고서 (2).txt"))
    ]
    store = DragCopyStore(files[6], tmp_path / "drag")
    try:
        batch = store.prepare(tuple(FileReference.attachment(i.id) for i in items), lambda: False)
        assert len({p.name.casefold() for p in batch.paths}) == 3
        assert [p.read_bytes() for p in batch.paths] == [b"0", b"1", b"2"]
        batch.paths[0].write_bytes(b"outside edit")
        assert files[4].resolve(items[0].relative_path).read_bytes() == b"0"
        batch.offered = True
        next_batch = store.prepare((FileReference.attachment(items[1].id),), lambda: False)
        assert batch.paths[0].exists()
        store.discard(next_batch)
        assert not next_batch.directory.exists()
    finally:
        store.close()
    assert batch.paths[0].exists()


@pytest.mark.parametrize("failure", ["missing", "limit", "cancel"])
def test_failed_batch_never_leaves_partial_ready_files(files, tmp_path, failure):  # noqa: F811
    items = [
        attach(files, tmp_path, name=f"f{i}.txt", content=f"data{i}".encode()) for i in range(2)
    ]
    store = DragCopyStore(files[6], tmp_path / "drag")
    if failure == "missing":
        files[4].resolve(items[1].relative_path).unlink()
    if failure == "limit":
        store.MAX_BYTES = 6
    count = [0]

    def canceled():
        count[0] += 1
        return failure == "cancel" and count[0] > 4

    try:
        with pytest.raises((AttachmentOperationError, AttachmentCanceledError, OSError)):
            store.prepare(tuple(FileReference.attachment(i.id) for i in items), canceled)
        assert not store._batches
        assert not list(store.session.rglob("*.txt"))
        assert not files[2].coordinator.is_pinned(items[0].relative_path)
    finally:
        store.close()


def test_frozen_original_and_modified_view_are_kept_separate(tmp_path):
    with lifecycle(tmp_path / "data") as (env, facade):
        item = original(env, facade)
        view = facade.prepare_open("attachment", item.id)
        view.write_bytes(b"edited viewing copy")
        service = AttachmentExportService(env.attachments, ExternalExportStorage(tmp_path / "data"))
        store = DragCopyStore(service, tmp_path / "drag")
        try:
            batch = store.prepare((FileReference.attachment(item.id),), lambda: False)
            assert batch.paths[0].read_bytes() == b"initial"
            (env.attachments.storage_root / item.relative_path).write_bytes(b"corrupt")
            with pytest.raises(AttachmentOperationError, match="고정"):
                store.prepare((FileReference.attachment(item.id),), lambda: False)
            assert view.read_bytes() == b"edited viewing copy"
        finally:
            store.close()


def test_cleanup_only_old_unlocked_owned_sessions(files, tmp_path):  # noqa: F811
    item = attach(files, tmp_path)
    refs = (FileReference.attachment(item.id),)
    first = DragCopyStore(files[6], tmp_path / "drag")
    batch = first.prepare(refs, lambda: False)
    first.offer(batch)
    marker = first.session / "lease.json"
    old = time.time() - 90000
    os.utime(marker, (old, old))
    os.utime(first.session / "last-offer", (old, old))
    unknown = first.root / "unknown"
    unknown.mkdir()
    (unknown / "lease.json").write_text(json.dumps({"owner": "someone else"}))
    os.utime(unknown / "lease.json", (old, old))
    second = DragCopyStore(files[6], first.root)
    try:
        second.prepare(refs, lambda: False)
        assert first.session.exists()  # First store still owns the OS read lease.
        first.close()
        (first.session / "last-offer").touch()
        third = DragCopyStore(files[6], first.root)
        try:
            third.prepare(refs, lambda: False)
            assert first.session.exists()  # A recent offer survives an old session age.
            os.utime(first.session / "last-offer", (old, old))
            fourth = DragCopyStore(files[6], first.root)
            try:
                fourth.prepare(refs, lambda: False)
                assert not first.session.exists()
            finally:
                fourth.close()
            assert unknown.exists()
        finally:
            third.close()
    finally:
        first.close()
        second.close()


def test_file_count_bound_before_copy(files, tmp_path):  # noqa: F811
    store = DragCopyStore(files[6], tmp_path / "drag")
    with pytest.raises(AttachmentOperationError):
        store.prepare(tuple(FileReference.attachment(i) for i in range(1, 102)), lambda: False)
    assert not store.session.exists()


def test_retained_batches_have_a_count_bound(files, tmp_path):  # noqa: F811
    item = attach(files, tmp_path)
    store = DragCopyStore(files[6], tmp_path / "drag")
    store.MAX_BATCHES = 1
    try:
        batch = store.prepare((FileReference.attachment(item.id),), lambda: False)
        store.offer(batch)
        with pytest.raises(AttachmentOperationError, match="한도"):
            store.prepare((FileReference.attachment(item.id),), lambda: False)
        assert batch.paths[0].exists()
    finally:
        store.close()
