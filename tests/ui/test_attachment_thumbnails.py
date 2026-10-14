import json
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event

import pytest
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.tasks import TaskDraft
from officeflow.domain.attachment import Attachment
from officeflow.infrastructure.settings.store import AppSettings, JsonSettingsStore
from officeflow.presentation import attachment_thumbnails as thumbnails
from officeflow.presentation.attachment_thumbnails import AttachmentImageCoordinator, visible_items
from officeflow.presentation.main_window import MainWindow
from tests.integration.test_attachment_search import search_environment
from tests.integration.test_document_lifecycle import lifecycle
from tests.ui.test_image_attachments import image, make_records


def attach_images(env, task, tmp_path, count=6, *, bulk_seed=False):
    values = []
    for index in range(count):
        path = tmp_path / f"capture_{index:03}.png"
        picture = image(QColor.fromHsv(index * 35 % 360, 180, 220))
        picture.setPixelColor(0, 0, QColor(index & 255, index >> 8 & 255, 123))
        assert picture.save(str(path))
        if bulk_seed:
            # UI load tests, not an import benchmark: real storage/files and rows,
            # without O(n²) duplicate checks while generating the synthetic fixture.
            stored = env.attachments._storage.import_file(path)
            values.append(
                env.repository.add_attachment(
                    Attachment(
                        id=None,
                        task_id=task.id,
                        original_name=path.name,
                        stored_name=stored.stored_name,
                        relative_path=stored.relative_path,
                        size_bytes=stored.size_bytes,
                        checksum=stored.checksum,
                        created_at=datetime.now(UTC),
                    )
                )
            )
        else:
            values.append(env.attachments.attach(task.id, path))
    return values


def test_thumbnail_mode_preserves_selection_order_and_external_open(qtbot, tmp_path, monkeypatch):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="Thumbnail selection"))
        attached = attach_images(env, task, tmp_path)
        records = make_records(env, task, qtbot)
        try:
            listing = records.attachment_list
            listing.item(1).setSelected(True)
            listing.item(3).setSelected(True)
            expected = {value.id for value in (attached[1], attached[3])}
            records.thumbnail_view_button.click()
            assert records.attachment_view_mode == "thumbnails"
            assert {
                item.data(Qt.ItemDataRole.UserRole) for item in listing.selectedItems()
            } == expected
            assert [
                listing.item(i).data(Qt.ItemDataRole.UserRole) for i in range(listing.count())
            ] == [a.id for a in attached]
            qtbot.waitUntil(lambda: records._image_coordinator.cache_bytes > 0)
            assert not records.image_preview_button.isVisible()
            records.list_view_button.click()
            assert records.attachment_view_mode == "list"
            assert {
                item.data(Qt.ItemDataRole.UserRole) for item in listing.selectedItems()
            } == expected
            opened = []
            monkeypatch.setattr(records, "_open_attachment", lambda: opened.append(True))
            records._open_clicked_attachment(listing.item(2))
            assert opened == [True]
        finally:
            records.shutdown()


def test_only_visible_thumbnails_are_read_and_list_does_not_decode(qtbot, tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="Lazy images"))
        attach_images(env, task, tmp_path, 30)
        records = make_records(env, task, qtbot)
        try:
            records.resize(500, 540)
            qtbot.wait(150)
            assert records._image_coordinator.read_count == 0
            records.thumbnail_view_button.click()
            qtbot.waitUntil(lambda: records._image_coordinator.cache_bytes > 0)
            qtbot.waitUntil(lambda: not records._image_coordinator.busy)
            assert records._image_coordinator.read_count < 30
            shown = visible_items(records.attachment_list)
            assert shown and len(shown) < 30
            assert all(
                records.attachment_list.visualItemRect(item).intersects(
                    records.attachment_list.viewport().rect()
                )
                for item in shown
            )
            records.attachment_list.scrollToBottom()
            qtbot.wait(250)
            assert any(
                item.data(Qt.ItemDataRole.UserRole)
                == records.attachment_list.item(29).data(Qt.ItemDataRole.UserRole)
                for item in visible_items(records.attachment_list)
            )
        finally:
            records.shutdown()


def test_shared_mode_next_window_not_forced_and_cache_released(qtbot, tmp_path):
    coordinator = AttachmentImageCoordinator()
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="Shared images"))
        attached = attach_images(env, task, tmp_path, 1)
        from officeflow.presentation.record_dialog import TaskRecordsDialog

        windows = []
        try:
            for _ in range(2):
                window = TaskRecordsDialog(
                    task,
                    task_service=env.tasks,
                    record_service=env.records,
                    attachment_service=env.attachments,
                    image_coordinator=coordinator,
                    initial_tab="attachments",
                )
                qtbot.addWidget(window)
                window.show()
                windows.append(window)
            windows[0].thumbnail_view_button.click()
            assert windows[1].attachment_view_mode == "list"
            assert coordinator.default_view == "thumbnails"
            qtbot.waitUntil(lambda: coordinator.cache_bytes > 0)
            assert coordinator.image_for(windows[0]._thumbnail_client, attached[0].id) is not None
            assert coordinator.cache_bytes <= coordinator.MAX_CACHE_BYTES
            for window in windows:
                window.shutdown()
            assert coordinator.cache_bytes == 0
        finally:
            for window in windows:
                window.shutdown()
            coordinator.shutdown()


@pytest.mark.parametrize("bad", [None, False, 1, [], {}, "invalid"])
def test_malformed_view_setting_preserves_other_settings(tmp_path, bad):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"attachment_view_mode": bad, "window_width": 999, "automatic_backup_keep": 7}),
        encoding="utf-8",
    )
    store = JsonSettingsStore(path)
    value = store.load()
    assert (
        value.attachment_view_mode == "list"
        and value.window_width == 999
        and value.automatic_backup_keep == 7
    )
    store.save(replace(value, attachment_view_mode="thumbnails"))
    assert store.load().attachment_view_mode == "thumbnails"


@pytest.mark.parametrize("failure", [False, True])
def test_main_window_shares_mode_with_next_records_and_preserves_settings(qtbot, tmp_path, failure):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="Settings"))
        saved = []

        def save(value):
            if failure:
                raise OSError("synthetic write failure")
            saved.append(value)

        window = MainWindow(
            AppSettings(automatic_backup_enabled=False, automatic_backup_keep=7),
            env.tasks,
            record_service=env.records,
            attachment_service=env.attachments,
            save_settings=save,
        )
        qtbot.addWidget(window)
        try:
            first = window._show_task_records(task, initial_tab="attachments")
            first.thumbnail_view_button.click()
            first.shutdown()
            second = window._show_task_records(task, initial_tab="attachments")
            assert second.attachment_view_mode == "thumbnails"
            assert window._settings.automatic_backup_keep == 7
            if failure:
                assert "저장하지 못" in window.statusBar().currentMessage()
            else:
                assert saved[-1].attachment_view_mode == "thumbnails"
        finally:
            window.shutdown()


def test_original_thumbnail_ignores_edited_view_and_rechecks_frozen_hash(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        task = env.tasks.create(TaskDraft(title="Original image"))
        attached = attach_images(env, task, tmp_path, 1)[0]
        facade.create(facade.catalog.get("attachment", attached.id), DocumentOperation.ORIGINAL)
        fixed = env.attachments.get_metadata(attached.id)
        records = make_records(env, task, qtbot)
        try:
            files = set(env.attachments.storage_root.rglob("*"))
            records.thumbnail_view_button.click()
            coordinator, client = records._image_coordinator, records._thumbnail_client
            qtbot.waitUntil(lambda: coordinator.image_for(client, fixed.id) is not None)
            old_color = coordinator.image_for(client, fixed.id).pixelColor(20, 20)
            assert env.attachments.get_metadata(fixed.id) == fixed
            assert set(env.attachments.storage_root.rglob("*")) == files
            view = facade.prepare_open("attachment", fixed.id)
            assert image("blue").save(str(view), "PNG")
            records._schedule_thumbnails(recheck=True)
            qtbot.waitUntil(
                lambda: not coordinator.busy and coordinator.image_for(client, fixed.id) is not None
            )
            assert coordinator.image_for(client, fixed.id).pixelColor(20, 20) == old_color
            assert image("green").save(
                str(env.attachments.storage_root / fixed.relative_path), "PNG"
            )
            records._schedule_thumbnails(recheck=True)
            qtbot.waitUntil(
                lambda: (
                    coordinator.state_for(client, fixed.id) is not None
                    and bool(coordinator.state_for(client, fixed.id).error)
                )
            )
            assert coordinator.image_for(client, fixed.id) is None
            assert coordinator.state_for(client, fixed.id).label == "고정 내용 불일치"
            assert env.attachments.get_metadata(fixed.id) == fixed
        finally:
            records.shutdown()


def test_edit_refresh_force_and_metadata_only_do_not_decode_again(qtbot, tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="Refresh"))
        attached = attach_images(env, task, tmp_path, 1)[0]
        records = make_records(env, task, qtbot)
        try:
            records.thumbnail_view_button.click()
            coordinator, client = records._image_coordinator, records._thumbnail_client
            qtbot.waitUntil(
                lambda: (
                    coordinator.image_for(client, attached.id) is not None and not coordinator.busy
                )
            )
            decoded = coordinator.decode_count
            records.select_attachment(attached.id)
            qtbot.wait(120)
            assert coordinator.decode_count == decoded
            env.attachments.rename_attachment(
                attached.id, "Renamed.png", expected_revision=attached.metadata_revision
            )
            qtbot.wait(250)
            qtbot.waitUntil(lambda: not coordinator.busy)
            assert coordinator.decode_count == decoded
            assert image("blue").save(
                str(env.attachments.storage_root / attached.relative_path), "PNG"
            )
            records._schedule_thumbnails(recheck=True)
            qtbot.waitUntil(
                lambda: (
                    coordinator.image_for(client, attached.id) is not None
                    and coordinator.image_for(client, attached.id).pixelColor(20, 20)
                    == QColor("blue")
                )
            )
            assert coordinator.decode_count == decoded + 1
            records._refresh_thumbnails()
            qtbot.waitUntil(lambda: coordinator.decode_count == decoded + 2)
        finally:
            records.shutdown()


@pytest.mark.parametrize("failure", ["missing", "corrupt", "permission"])
def test_thumbnail_failure_is_inline_does_not_modify_metadata_or_retry_on_paint(
    qtbot, tmp_path, monkeypatch, failure
):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="Inline error"))
        attached = attach_images(env, task, tmp_path, 1)[0]
        path = env.attachments.storage_root / attached.relative_path
        if failure == "missing":
            path.unlink()
        elif failure == "corrupt":
            path.write_bytes(b"synthetic invalid image")
        else:
            monkeypatch.setattr(
                env.attachments._storage,
                "protected_read",
                lambda _path: (_ for _ in ()).throw(PermissionError("synthetic denied")),
            )
        records = make_records(env, task, qtbot)
        try:
            records.thumbnail_view_button.click()
            coordinator, client = records._image_coordinator, records._thumbnail_client
            qtbot.waitUntil(
                lambda: (
                    coordinator.state_for(client, attached.id) is not None
                    and bool(coordinator.state_for(client, attached.id).error)
                )
            )
            assert coordinator.image_for(client, attached.id) is None
            reads = coordinator.read_count
            for _ in range(5):
                records.attachment_list.viewport().update()
                records._schedule_thumbnails()
                qtbot.wait(120)
            assert coordinator.read_count == reads
            assert env.attachments.get_metadata(attached.id) == attached
        finally:
            records.shutdown()


def test_shared_decode_queue_cancels_before_file_command_and_stays_responsive(
    qtbot, tmp_path, monkeypatch
):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="Cancel and command"))
        attached = attach_images(env, task, tmp_path, 1)[0]
        started, stopped = Event(), Event()
        read = thumbnails.read_bounded_image

        def slow(*args, **kwargs):
            started.set()
            while not args[2]():
                Event().wait(0.005)
            stopped.set()
            return read(*args, **kwargs)

        monkeypatch.setattr(thumbnails, "read_bounded_image", slow)
        records = make_records(env, task, qtbot)
        pulses = []
        timer = QTimer()
        timer.timeout.connect(lambda: pulses.append(True))
        timer.start(10)
        try:
            records.select_attachment(attached.id)
            records.thumbnail_view_button.click()
            qtbot.waitUntil(started.is_set)
            qtbot.wait(40)
            completed = []
            assert records._defer_image_action(lambda: completed.append(stopped.is_set()))
            qtbot.waitUntil(lambda: completed == [True])
            assert len(pulses) >= 3
            assert records._image_coordinator.cache_bytes == 0
        finally:
            timer.stop()
            records.shutdown()


def test_queue_and_lru_have_hard_count_and_byte_bounds(qtbot, tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="Cache bounds"))
        attached = attach_images(env, task, tmp_path, 70)
        coordinator = AttachmentImageCoordinator()
        clients = [coordinator.register() for _ in range(4)]
        try:
            for index, client in enumerate(clients):
                coordinator.sync(
                    client,
                    tuple(
                        (env.attachments, a, QSize(640, 400))
                        for a in attached[index * 20 : (index + 1) * 20]
                    ),
                )
                assert coordinator.pending_count <= 32
            qtbot.waitUntil(lambda: not coordinator.busy, timeout=15000)
            assert (
                coordinator.cache_entries == 64
                and coordinator.cache_bytes <= coordinator.MAX_CACHE_BYTES
            )
            coordinator.MAX_CACHE_BYTES = 10000
            coordinator.sync(
                clients[0], ((env.attachments, attached[0], QSize(640, 400)),), epoch=1, force=True
            )
            qtbot.waitUntil(lambda: not coordinator.busy)
            assert coordinator.cache_bytes <= 10000
        finally:
            coordinator.shutdown()


@pytest.mark.parametrize("count", [100, 1000])
def test_large_attachment_set_reads_only_visible_ids_and_grid_helper_matches(
    qtbot, tmp_path, count
):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="Large attachment list"))
        attach_images(env, task, tmp_path, count, bulk_seed=True)
        records = make_records(env, task, qtbot)
        try:
            records.resize(500, 540)
            records.thumbnail_view_button.click()
            qtbot.waitUntil(lambda: records._image_coordinator.cache_bytes > 0)
            assert records._image_coordinator.read_count < 12
            for position in (
                0,
                records.attachment_list.verticalScrollBar().maximum() // 2,
                records.attachment_list.verticalScrollBar().maximum(),
            ):
                records.attachment_list.verticalScrollBar().setValue(position)
                qtbot.wait(130)
                listing = records.attachment_list
                expected = tuple(
                    listing.item(i)
                    for i in range(listing.count())
                    if listing.visualItemRect(listing.item(i)).intersects(listing.viewport().rect())
                )
                assert visible_items(listing) == expected
                assert records._image_coordinator.pending_count <= 32
        finally:
            records.shutdown()


def test_same_visible_request_is_coalesced_and_refocus_is_verified_again(qtbot, tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="Coalesced"))
        attached = attach_images(env, task, tmp_path, 1)[0]
        coordinator = AttachmentImageCoordinator()
        first, second = coordinator.register(), coordinator.register()
        try:
            values = ((env.attachments, attached, QSize(200, 104)),)
            coordinator.sync(first, values, epoch=1)
            coordinator.sync(second, values, epoch=1)
            qtbot.waitUntil(lambda: not coordinator.busy)
            assert coordinator.read_count == coordinator.decode_count == 1
            assert (
                coordinator.image_for(first, attached.id) is not None
                and coordinator.image_for(second, attached.id) is not None
            )
            coordinator.sync(second, values, epoch=2)
            qtbot.waitUntil(lambda: not coordinator.busy)
            assert coordinator.read_count == 2 and coordinator.decode_count == 1
        finally:
            coordinator.shutdown()


def test_changing_view_and_closing_discards_late_image_result(qtbot, tmp_path, monkeypatch):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="Stale result"))
        attached = attach_images(env, task, tmp_path, 1)[0]
        started, release = Event(), Event()
        read = thumbnails.read_bounded_image

        def paused(*args, **kwargs):
            value = read(*args, **kwargs)
            started.set()
            assert release.wait(5)
            return value

        monkeypatch.setattr(thumbnails, "read_bounded_image", paused)
        records = make_records(env, task, qtbot)
        try:
            records.thumbnail_view_button.click()
            qtbot.waitUntil(started.is_set)
            records.list_view_button.click()
            release.set()
            qtbot.waitUntil(lambda: not records._image_coordinator.busy)
            assert (
                records._image_coordinator.image_for(records._thumbnail_client, attached.id) is None
            )
            assert records._image_coordinator.cache_bytes == 0
            records.shutdown()
            assert records._image_coordinator._thread is None
        finally:
            release.set()
            records.shutdown()


def test_other_tab_hidden_minimized_and_document_only_do_not_read_images(qtbot, tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="Visibility"))
        attach_images(env, task, tmp_path, 1)
        records = make_records(env, task, qtbot)
        try:
            records.select_tab("checklist")
            records.thumbnail_view_button.click()
            qtbot.wait(150)
            assert records._image_coordinator.read_count == 0
            records.hide()
            records.select_tab("attachments")
            qtbot.wait(150)
            assert records._image_coordinator.read_count == 0
            records.showMinimized()
            qtbot.wait(150)
            assert records._image_coordinator.read_count == 0
            records.showNormal()
            qtbot.waitUntil(lambda: records._image_coordinator.cache_bytes > 0)
        finally:
            records.shutdown()


def test_scroll_anchor_multi_selection_and_drop_survive_mode_change(qtbot, tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="Scroll anchor"))
        attach_images(env, task, tmp_path, 12)
        records = make_records(env, task, qtbot)
        try:
            listing = records.attachment_list
            records.resize(500, 540)
            listing.scrollToBottom()
            qtbot.wait(30)
            anchor = records._attachment_scroll_anchor()
            listing.item(10).setSelected(True)
            records.thumbnail_view_button.click()
            qtbot.wait(150)
            assert anchor in {
                item.data(Qt.ItemDataRole.UserRole) for item in visible_items(listing)
            }
            assert listing.acceptDrops() and not listing.dragEnabled()
            assert listing.item(10).isSelected()
            listing.selectAll()
            selected = {item.data(Qt.ItemDataRole.UserRole) for item in listing.selectedItems()}
            records.list_view_button.click()
            assert {
                item.data(Qt.ItemDataRole.UserRole) for item in listing.selectedItems()
            } == selected
            assert listing.acceptDrops() and not listing.dragEnabled()
        finally:
            records.shutdown()
