from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QMimeData
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.attachment_export import (
    AttachmentExportService,
    ExportStatus,
    FileReference,
)
from officeflow.application.attachments import AttachmentOperationError
from officeflow.application.file_operations import FileJobCanceledError
from officeflow.application.tasks import TaskDraft, TaskQuery, TaskView
from officeflow.bootstrap.paths import AppPaths
from officeflow.domain.enums import ReminderRelation
from officeflow.domain.reminder import ReminderRuleInput
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation import image_attachments as images
from officeflow.presentation.image_attachments import (
    ClipboardImagePanel,
    DraftImage,
    ImageImportResult,
    ImagePreviewDialog,
    check_image,
    import_images,
    read_preview,
)
from officeflow.presentation.main_window import MainWindow
from officeflow.presentation.record_dialog import TaskRecordsDialog
from tests.integration.test_attachment_search import search_environment
from tests.integration.test_document_lifecycle import lifecycle
from tests.ui.test_multiple_reminders_ui import live_window  # noqa: F401
from tests.ui.test_record_notification_interaction import (
    native_click,
    open_record_window,
    record_window,  # noqa: F401
)


def image(color="red"):
    value = QImage(120, 80, QImage.Format.Format_ARGB32)
    value.fill(QColor(color))
    return value


@pytest.fixture
def clipboard(qapp, monkeypatch):
    mime = QMimeData()
    value = image()
    mime.setImageData(value)
    fake = SimpleNamespace(value=value, mime=mime)
    # Prime Qt's lazily installed binding; never read or replace user clipboard data.
    QApplication.clipboard()
    monkeypatch.setattr(
        QApplication,
        "clipboard",
        lambda: SimpleNamespace(mimeData=lambda: fake.mime, image=lambda: fake.value),
    )
    return fake


def panel(qtbot):
    widget = ClipboardImagePanel()
    qtbot.addWidget(widget)
    widget.show()
    return widget


def test_clipboard_snapshot_name_preview_remove_and_unchanged_clipboard(qtbot, clipboard):
    widget = panel(qtbot)
    widget.add_button.click()
    first = widget.entries()[0]
    assert first.name.startswith("캡처_") and first.name.endswith(".png")
    assert widget.thumbnail.pixmap() is not None
    clipboard.value.fill(QColor("blue"))
    assert first.image.pixelColor(0, 0) == QColor("red")
    widget.add_button.click()
    assert len(widget.entries()) == 2
    assert widget.entries()[0].name != widget.entries()[1].name
    widget.selector.setCurrentIndex(0)
    widget.name_edit.setFocus()
    widget.name_edit.selectAll()
    qtbot.keyClicks(widget.name_edit, "result")
    assert widget.entries()[0].name == "result.png"
    widget.preview_button.click()
    assert widget._preview.isVisible() and not widget._preview.isModal()
    widget.remove_button.click()
    assert len(widget.entries()) == 1
    assert clipboard.value.pixelColor(0, 0) == QColor("blue")
    widget.shutdown()


def test_clipboard_nonimage_invalid_name_and_bounds(qtbot, clipboard, monkeypatch):
    widget = panel(qtbot)
    clipboard.mime = QMimeData()
    clipboard.mime.setText("not an image")
    widget.add_clipboard()
    assert not widget.has_images and "이미지가 없습니다" in widget.feedback.text()
    clipboard.mime.setImageData(clipboard.value)
    for _ in range(8):
        widget.add_clipboard()
    widget.add_clipboard()
    assert len(widget.entries()) == 8 and "8장" in widget.feedback.text()
    widget.name_edit.setText("../bad.png")
    widget._rename("../bad.png")
    with pytest.raises(ValueError):
        widget.entries()
    monkeypatch.setattr(images, "MAX_IMAGE_PIXELS", 1)
    with pytest.raises(ValueError, match="너무 큽니다"):
        check_image(clipboard.value)
    widget.shutdown()


def test_import_and_preview_are_general_readonly_and_duplicate_safe(qtbot, tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="사진 첨부"))
        entry = DraftImage("a", "오류.png", image())
        result = import_images(env.attachments, task.id, (entry,), lambda: False)
        assert result.completed == ("a",) and not result.errors
        attachments = env.attachments.attachments_for_task(task.id)
        assert len(attachments) == 1 and not attachments[0].is_frozen
        before = attachments[0]
        assert read_preview(env.attachments, before.id, lambda: False).pixelColor(0, 0) == QColor(
            "red"
        )
        assert env.attachments.get_metadata(before.id) == before
        repeat = import_images(env.attachments, task.id, (entry,), lambda: False)
        assert repeat.completed == ("a",) and repeat.duplicates == 1
        assert "이미 첨부" in repeat.notice
        assert len(env.attachments.attachments_for_task(task.id)) == 1


def test_image_partial_import_keeps_failed_and_cleans_temp(qtbot, tmp_path, monkeypatch):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="부분 성공"))
        attach = env.attachments.attach
        paths = []

        def fail_one(task_id, path, **kwargs):
            paths.append(path)
            if path.name == "second.png":
                raise OSError("synthetic disk failure")
            return attach(task_id, path, **kwargs)

        monkeypatch.setattr(env.attachments, "attach", fail_one)
        entries = (
            DraftImage("a", "first.png", image()),
            DraftImage("b", "second.png", image("blue")),
        )
        result = import_images(env.attachments, task.id, entries, lambda: False)
        assert result.completed == ("a",) and "synthetic" in result.errors[0]
        assert all(not p.exists() and not p.parent.exists() for p in paths)
        canceled = import_images(env.attachments, task.id, entries, lambda: True)
        assert not canceled.completed and canceled.errors


@pytest.mark.parametrize("mode", ["bad", "missing", "wrong-format", "too-large"])
def test_preview_failure_keeps_metadata_and_files(qtbot, tmp_path, monkeypatch, mode):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="미리보기 오류"))
        source = tmp_path / "picture.png"
        source.write_bytes(b"not an image")
        item = env.attachments.attach(task.id, source)
        path = env.attachments.storage_root / item.relative_path
        if mode == "missing":
            path.unlink()
        elif mode == "wrong-format":
            value = image()
            assert value.save(str(path), "PPM")
        elif mode == "too-large":
            assert image().save(str(path), "PNG")
            monkeypatch.setattr(images, "MAX_IMAGE_PIXELS", 1)
        with pytest.raises((ValueError, OSError)):
            read_preview(env.attachments, item.id, lambda: False)
        assert env.attachments.get_metadata(item.id) == item


def open_editor(window):
    window._open_editor(None)
    editor = window._file_dialogs[-1]
    editor.title_edit.setText("사진 새 업무")
    editor.image_panel.add_clipboard()
    return editor


def test_new_task_and_image_save_together_or_cancel(qtbot, clipboard, tmp_path):
    with search_environment(tmp_path) as env:
        window = MainWindow(
            AppSettings(automatic_backup_enabled=False),
            env.tasks,
            record_service=env.records,
            attachment_service=env.attachments,
        )
        qtbot.addWidget(window)
        try:
            editor = open_editor(window)
            editor.reject()
            assert not env.tasks.list(TaskQuery(view=TaskView.ALL))
            editor = open_editor(window)
            editor._validate_and_accept()
            qtbot.waitUntil(lambda: not isValid(editor) or not editor.isVisible(), timeout=10000)
            tasks = env.tasks.list(TaskQuery(view=TaskView.ALL))
            assert len(tasks) == 1
            assert len(env.attachments.attachments_for_task(tasks[0].id)) == 1
        finally:
            window.shutdown()


def test_task_saved_image_failure_retry_and_invalid_name_do_not_duplicate(
    qtbot, clipboard, tmp_path, monkeypatch
):
    with search_environment(tmp_path) as env:
        window = MainWindow(
            AppSettings(automatic_backup_enabled=False),
            env.tasks,
            record_service=env.records,
            attachment_service=env.attachments,
        )
        qtbot.addWidget(window)
        attach = env.attachments.attach
        try:
            editor = open_editor(window)
            monkeypatch.setattr(
                env.attachments,
                "attach",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic")),
            )
            editor._validate_and_accept()
            qtbot.waitUntil(lambda: not editor._image_controller.busy)
            assert editor.isVisible() and editor.image_panel.has_images
            assert editor._saved_image_task is not None
            assert "이미 저장" in editor.error_label.text()
            assert not editor.title_edit.isEnabled()
            editor.image_panel._rename("../bad.png")
            editor._validate_and_accept()
            assert len(env.tasks.list(TaskQuery(view=TaskView.ALL))) == 1
            editor.image_panel._rename("retry.png")
            monkeypatch.setattr(env.attachments, "attach", attach)
            editor._validate_and_accept()
            qtbot.waitUntil(lambda: not isValid(editor) or not editor.isVisible(), timeout=10000)
            tasks = env.tasks.list(TaskQuery(view=TaskView.ALL))
            assert len(tasks) == 1 and len(env.attachments.attachments_for_task(tasks[0].id)) == 1
        finally:
            window.shutdown()


def make_records(env, task, qtbot):
    widget = TaskRecordsDialog(
        task,
        task_service=env.tasks,
        record_service=env.records,
        attachment_service=env.attachments,
        initial_tab="attachments",
    )
    qtbot.addWidget(widget)
    widget.show()
    return widget


def test_record_clipboard_and_preview_selection_dont_shrink_list(qtbot, clipboard, tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="기존 사진"))
        records = make_records(env, task, qtbot)
        try:
            records.clipboard_image_button.click()
            child = records._clipboard_dialog
            assert child.isVisible() and not child.isModal()
            child._save()
            qtbot.waitUntil(lambda: records._clipboard_dialog is None, timeout=10000)
            qtbot.waitUntil(lambda: records.attachment_list.count() == 1)
            first_id = env.attachments.attachments_for_task(task.id)[0].id
            records.select_attachment(first_id)
            height = records.attachment_list.height()
            records.image_preview_button.click()
            viewer = records._image_preview
            qtbot.waitUntil(lambda: not viewer._image.isNull(), timeout=10000)
            assert records.attachment_list.height() == height
            assert viewer._image.pixelColor(0, 0) == QColor("red")
            clipboard.value = image("blue")
            records.clipboard_image_button.click()
            records._clipboard_dialog._save()
            qtbot.waitUntil(lambda: records._clipboard_dialog is None, timeout=10000)
            items = env.attachments.attachments_for_task(task.id)
            records.select_attachment(next(item.id for item in items if item.id != first_id))
            assert len(records.attachment_list.selectedItems()) == 1
            assert viewer.isVisible()
            qtbot.waitUntil(
                lambda: (
                    not viewer._image.isNull() and viewer._image.pixelColor(0, 0) == QColor("blue")
                ),
                timeout=10000,
            )
            records.attachment_list.clearSelection()
            assert viewer._image.isNull()
            records.select_attachment(first_id)
            qtbot.waitUntil(lambda: not viewer._image.isNull())
            next(
                records.attachment_list.item(i)
                for i in range(records.attachment_list.count())
                if not records.attachment_list.item(i).isSelected()
            ).setSelected(True)
            assert viewer._image.isNull()
            viewer.reject()
            records.select_attachment(first_id)
            records.image_preview_button.click()
            qtbot.waitUntil(lambda: not viewer._image.isNull())
        finally:
            records.shutdown()


def test_original_preview_verifies_without_view_cache(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        task = env.tasks.create(TaskDraft(title="고정 사진"))
        source = tmp_path / "fixed.png"
        assert image().save(str(source))
        item = env.attachments.attach(task.id, source)
        facade.create(facade.catalog.get("attachment", item.id), DocumentOperation.ORIGINAL)
        fixed = env.attachments.get_metadata(item.id)
        files_before = set(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
        assert not read_preview(env.attachments, fixed.id, lambda: False).isNull()
        assert env.attachments.get_metadata(fixed.id) == fixed
        assert files_before == set(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
        path = env.attachments.storage_root / fixed.relative_path
        assert image("blue").save(str(path), "PNG")
        with pytest.raises(AttachmentOperationError, match="변경된 원본"):
            read_preview(env.attachments, fixed.id, lambda: False)


@pytest.mark.parametrize("attempt", range(10))
def test_image_import_shutdown_waits_cancel_and_leaves_no_task_duplicate(
    qtbot, clipboard, tmp_path, monkeypatch, attempt
):
    with search_environment(tmp_path) as env:
        window = MainWindow(
            AppSettings(automatic_backup_enabled=False),
            env.tasks,
            record_service=env.records,
            attachment_service=env.attachments,
        )
        qtbot.addWidget(window)
        started, stopped = Event(), Event()

        def slow(_task_id, _path, *, cancel_requested):
            started.set()
            while not cancel_requested():
                Event().wait(0.01)
            stopped.set()
            raise OSError("cancel")

        monkeypatch.setattr(env.attachments, "attach", slow)
        editor = open_editor(window)
        editor._validate_and_accept()
        qtbot.waitUntil(started.is_set)
        window.shutdown()
        assert stopped.is_set()
        assert len(env.tasks.list(TaskQuery(view=TaskView.ALL))) == 1


def test_preview_late_result_is_discarded_and_shutdown_joins(qtbot, monkeypatch):
    started = Event()

    def slow(_service, _id, canceled):
        started.set()
        while not canceled():
            Event().wait(0.01)
        return image()

    monkeypatch.setattr(images, "read_preview", slow)
    viewer = ImagePreviewDialog()
    qtbot.addWidget(viewer)
    viewer.show()
    viewer.load(None, 1, "old.png")
    qtbot.waitUntil(started.is_set)
    viewer.clear()
    qtbot.waitUntil(lambda: viewer._thread is None)
    assert viewer._image.isNull()
    viewer.load(None, 2, "next.png")
    qtbot.waitUntil(lambda: viewer._thread is not None)
    viewer.reject()
    assert viewer._image.isNull()


def test_clipboard_null_and_total_memory_bound(qtbot, clipboard, monkeypatch):
    widget = panel(qtbot)
    clipboard.value = QImage()
    widget.add_clipboard()
    assert not widget.has_images
    clipboard.value = image()
    monkeypatch.setattr(images, "MAX_PENDING_BYTES", clipboard.value.sizeInBytes())
    widget.add_clipboard()
    widget.add_clipboard()
    assert len(widget.entries()) == 1 and "128 MB" in widget.feedback.text()
    widget.shutdown()


def test_many_long_errors_remain_compact():
    result = ImageImportResult((), tuple("error " * 200 for _ in range(8)))
    assert len(result.error_summary) < 190
    assert "추가 오류 7건" in result.error_summary
    assert ImageImportResult((), ()).error_summary == ""


def test_trashed_task_preview_remains_readonly(qtbot, tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="휴지통 사진"))
        import_images(
            env.attachments, task.id, (DraftImage("a", "image.png", image()),), lambda: False
        )
        env.tasks.move_to_trash(task.id)
        records = make_records(env, env.tasks.get_including_deleted(task.id), qtbot)
        try:
            assert not records.clipboard_image_button.isEnabled()
            records.select_attachment(env.attachments.attachments_for_task(task.id)[0].id)
            assert records.image_preview_button.isEnabled()
            records.image_preview_button.click()
            qtbot.waitUntil(lambda: not records._image_preview._image.isNull())
        finally:
            records.shutdown()


def test_preview_size_permission_and_cancel_are_readonly(qtbot, tmp_path, monkeypatch):
    from officeflow.application import attachments as attachment_module

    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="읽기 한도"))
        result = import_images(
            env.attachments, task.id, (DraftImage("a", "image.png", image()),), lambda: False
        )
        assert result.completed
        item = env.attachments.attachments_for_task(task.id)[0]
        with monkeypatch.context() as m:
            m.setattr(attachment_module, "MAX_IMAGE_BYTES", 1)
            with pytest.raises(AttachmentOperationError, match="64"):
                read_preview(env.attachments, item.id, lambda: False)
        with pytest.raises(FileJobCanceledError, match="취소"):
            read_preview(env.attachments, item.id, lambda: True)
        with monkeypatch.context() as m:
            m.setattr(
                env.attachments._storage,
                "protected_read",
                lambda _p: (_ for _ in ()).throw(PermissionError("synthetic denial")),
            )
            with pytest.raises(PermissionError):
                read_preview(env.attachments, item.id, lambda: False)
        assert env.attachments.get_metadata(item.id) == item
        assert not read_preview(env.attachments, item.id, lambda: False).isNull()


@pytest.mark.parametrize("suffix", ["png", "jpg", "jpeg", "bmp"])
def test_preview_supported_formats_and_window_error(qtbot, tmp_path, suffix):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="지원 이미지"))
        source = tmp_path / f"capture.{suffix}"
        assert image().save(str(source))
        item = env.attachments.attach(task.id, source)
        result = read_preview(env.attachments, item.id, lambda: False)
        assert result.size() == image().size()
        viewer = ImagePreviewDialog()
        qtbot.addWidget(viewer)
        viewer.show()
        viewer.load(env.attachments, item.id, item.name)
        qtbot.waitUntil(lambda: not viewer._image.isNull())
        viewer.load(env.attachments, 99999, "missing.png")
        qtbot.waitUntil(lambda: viewer._thread is None)
        assert viewer._image.isNull() and viewer.picture.text()
        viewer.reject()


def test_encoding_and_database_failure_leave_no_attachment(qtbot, tmp_path, monkeypatch):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="실패 보호"))
        entries = (DraftImage("a", "image.png", QImage()),)
        result = import_images(env.attachments, task.id, entries, lambda: False)
        assert not result.completed and "PNG" in result.errors[0]
        monkeypatch.setattr(
            env.attachments._repository,
            "add_attachment",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("synthetic db")),
        )
        result = import_images(
            env.attachments, task.id, (DraftImage("a", "image.png", image()),), lambda: False
        )
        assert not result.completed and "synthetic db" in result.errors[0]
        assert not env.attachments.attachments_for_task(task.id)
        assert not list(env.attachments.storage_root.rglob("*.png"))


def test_partial_clipboard_save_retains_only_failure(qtbot, clipboard, tmp_path, monkeypatch):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="부분 저장"))
        records = make_records(env, task, qtbot)
        try:
            records.clipboard_image_button.click()
            child = records._clipboard_dialog
            clipboard.value = image("blue")
            child.panel.add_clipboard()
            entries = child.panel.entries()
            attach = env.attachments.attach

            def fail_second(task_id, path, **kwargs):
                if path.name == entries[1].name:
                    raise OSError("synthetic")
                return attach(task_id, path, **kwargs)

            monkeypatch.setattr(env.attachments, "attach", fail_second)
            child._save()
            qtbot.waitUntil(lambda: not child.controller.busy)
            assert child.isVisible()
            assert len(child.panel.entries()) == 1
            assert child.panel.entries()[0].key == entries[1].key
            assert len(env.attachments.attachments_for_task(task.id)) == 1
            monkeypatch.setattr(env.attachments, "attach", attach)
            child._save()
            qtbot.waitUntil(lambda: records._clipboard_dialog is None)
            assert len(env.attachments.attachments_for_task(task.id)) == 2
        finally:
            records.shutdown()


def test_image_backup_restore_and_external_copy(qtbot, tmp_path):
    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        task = env.tasks.create(TaskDraft(title="백업 사진"))
        import_images(
            env.attachments, task.id, (DraftImage("a", "capture.png", image()),), lambda: False
        )
        item = env.attachments.attachments_for_task(task.id)[0]
        export = AttachmentExportService(
            env.attachments, ExternalExportStorage(tmp_path / "export-state")
        )
        destination = tmp_path / "saved"
        destination.mkdir()
        result = export.export((FileReference.attachment(item.id),), destination)
        assert result.items[0].status is ExportStatus.SAVED
        assert QImage(str(result.items[0].output_path)).pixelColor(0, 0) == QColor("red")
        manager = BackupManager(paths, coordinator=env.attachments.coordinator)
        backup = manager.create_backup()
        assert manager.verify_backup(backup.path).attachments
    manager.stage_restore(backup.path)
    manager.apply_pending_restore()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        assert read_preview(env.attachments, item.id, lambda: False).pixelColor(0, 0) == QColor(
            "red"
        )


def test_task_retry_keeps_production_reminder_identity(live_window, qtbot, clipboard, monkeypatch):  # noqa: F811
    window, env, task, _now, repo = live_window
    before = tuple(repo.list_reminders(task.id))
    window._open_editor(task)
    editor = window._file_dialogs[-1]
    editor.image_panel.add_clipboard()
    attach = env.attachments.attach
    monkeypatch.setattr(
        env.attachments, "attach", lambda *_a, **_k: (_ for _ in ()).throw(OSError("synthetic"))
    )
    editor._validate_and_accept()
    qtbot.waitUntil(lambda: not editor._image_controller.busy)
    assert tuple(repo.list_reminders(task.id)) == before
    monkeypatch.setattr(env.attachments, "attach", attach)
    editor._validate_and_accept()
    qtbot.waitUntil(lambda: not isValid(editor) or not editor.isVisible())
    assert tuple(repo.list_reminders(task.id)) == before
    assert len(env.tasks.list(TaskQuery(view=TaskView.ALL))) == 1


@pytest.mark.parametrize("operation", ["clipboard", "preview"])
def test_native_reminder_usable_with_image_windows(record_window, qtbot, clipboard, operation):  # noqa: F811
    window, env, task_id, reminders = record_window
    import_images(
        env.attachments, task_id, (DraftImage("a", "capture.png", image()),), lambda: False
    )
    records = open_record_window(window, task_id, "list-attachments", qtbot)
    if operation == "clipboard":
        records.clipboard_image_button.click()
        child = records._clipboard_dialog
    else:
        item = next(
            a for a in env.attachments.attachments_for_task(task_id) if a.name == "capture.png"
        )
        records.select_attachment(item.id)
        records.image_preview_button.click()
        child = records._image_preview
        qtbot.waitUntil(lambda: not child._image.isNull())
    assert child.isVisible() and not child.isModal()
    reminders.replace_rules(task_id, (ReminderRuleInput(ReminderRelation.START, 0),))
    window._check_reminders()
    alert = window._reminder_dialog
    assert alert is not None and alert.isVisible()
    native_click(alert, alert.acknowledge_button, qtbot)
    qtbot.waitUntil(lambda: window._reminder_dialog is None)
    assert child.isVisible()
