"""U23: visible selections, compact browsing, source metadata and normal window sizes."""

import pytest
from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QPlainTextEdit

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.domain.attachment import AttachmentMove
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.document_actions import DocumentBrowser, file_actions
from officeflow.presentation.main_window import MainWindow
from tests.integration.test_document_lifecycle import lifecycle, original
from tests.ui.test_attachment_feedback import make_records


def mixed_files(env, service, root):
    first = original(env, service)
    originals = [first]
    for n in (2, 3):
        source = root / f"original-{n}.txt"
        source.write_bytes(f"source-{n}".encode())
        item = env.attachments.attach(first.task_id, source)
        service.create(service.catalog.get("attachment", item.id), DocumentOperation.ORIGINAL)
        originals.append(env.attachments.get_metadata(item.id))
    works = [
        env.attachments.get_metadata(
            service.create(
                service.catalog.get("attachment", item.id),
                DocumentOperation.WORK,
                filename=f"work-{n}.txt",
            ).attachment_id
        )
        for n, item in enumerate(originals[:2], start=1)
    ]
    return originals, works


def choose_kind(dialog, kind):
    dialog.attachment_kind_combo.setCurrentIndex(dialog.attachment_kind_combo.findData(kind))


def test_kind_filter_drops_hidden_selections_and_keeps_global_order(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, service):
        originals, works = mixed_files(env, service, tmp_path)
        dialog = make_records(env, originals[0], tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        before = [
            item.id for item in env.attachments.attachment_metadata_for_task(originals[0].task_id)
        ]
        for row in (0, 1, 3):
            dialog.attachment_list.selectionModel().select(
                dialog.attachment_list.model().index(row, 0),
                QItemSelectionModel.SelectionFlag.Select,
            )
        choose_kind(dialog, "work")
        assert dialog.attachment_list.count() == 2
        assert [item.id for item in dialog._visible_attachments()] == [item.id for item in works]
        assert dialog._selected_attachment_ids() == (works[0].id,)
        assert dialog.clear_attachment_filter.isVisible()
        assert "전체 5" in dialog.attachment_filter_status.text()
        assert not dialog._attachment_move_enabled(AttachmentMove.UP)
        choose_kind(dialog, "all")
        assert dialog._selected_attachment_ids() == (works[0].id,)
        assert [
            item.id for item in env.attachments.attachment_metadata_for_task(originals[0].task_id)
        ] == before
        dialog.shutdown()


def test_filtered_save_and_delete_only_take_visible_ids(qtbot, tmp_path, monkeypatch):
    with lifecycle(tmp_path) as (env, service):
        originals, works = mixed_files(env, service, tmp_path)
        dialog = make_records(env, originals[0], tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.attachment_list.selectAll()
        choose_kind(dialog, "work")
        saved, deleted = [], []
        monkeypatch.setattr(dialog._export_controller, "choose", lambda refs: saved.extend(refs))
        monkeypatch.setattr(
            file_actions(dialog, service),
            "delete_many",
            lambda _task, targets, _done: deleted.extend(targets),
        )
        dialog._save_attachment_copy()
        dialog._delete_attachment()
        assert [ref.attachment_id for ref in saved] == [item.id for item in works]
        assert [(target.kind, target.id) for target in deleted] == [
            ("attachment", item.id) for item in works
        ]
        dialog.shutdown()


def test_selected_work_source_is_async_and_available_in_information(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, service):
        originals, works = mixed_files(env, service, tmp_path)
        dialog = make_records(env, originals[0], tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.select_attachment(works[0].id)
        qtbot.waitUntil(lambda: "출처:" in dialog.attachment_detail.full_text)
        assert originals[0].name in dialog.attachment_detail.full_text
        dialog._show_attachment_information()
        info = file_actions(dialog, service).dialogs[-1].findChild(QPlainTextEdit).toPlainText()
        assert "출처" in info and originals[0].name in info
        dialog.shutdown()


def test_empty_kind_and_external_selection_are_not_confused_with_no_files(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        dialog = make_records(env, item, tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        choose_kind(dialog, "work")
        assert dialog.attachment_list.count() == 0
        assert "전체 첨부는 1개" in dialog.attachment_empty_label.text()
        dialog.select_attachment(item.id)
        assert dialog.attachment_kind_combo.currentData() == "all"
        assert dialog._selected_attachment_ids() == (item.id,)
        dialog.shutdown()


def test_related_files_reuses_browser_with_explicit_current_attachment_scope(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, service):
        originals, works = mixed_files(env, service, tmp_path)
        dialog = make_records(env, originals[0], tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.select_attachment(works[0].id)
        dialog._open_related_files()
        browser = next(
            box for box in file_actions(dialog, service).dialogs if isinstance(box, DocumentBrowser)
        )
        assert browser.kind.currentIndex() == 1
        assert browser.family_id == works[0].family_id
        assert "선택 파일" in browser.scope_label.full_text
        assert browser.list.count() == 2
        browser.shutdown()
        dialog.shutdown()


def test_wide_view_preserves_selection_and_restores_same_window(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, service):
        originals, works = mixed_files(env, service, tmp_path)
        dialog = make_records(env, originals[0], tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.select_attachment(works[0].id)
        before = dialog.size()
        notices = []
        dialog.windowSizeChanged.connect(lambda w, h: notices.append((w, h)))
        dialog.wide_attachment_button.click()
        assert dialog.width() >= before.width() and not dialog.isFullScreen()
        assert dialog._selected_attachment_ids() == (works[0].id,)
        assert dialog.wide_attachment_button.text() == "이전 크기"
        dialog.wide_attachment_button.click()
        assert dialog.size() == before
        dialog.shutdown()
        assert notices == [(before.width(), before.height())]


def test_window_geometry_is_saved_using_latest_settings_without_replacing_other_values(
    qtbot, tmp_path
):
    from dataclasses import replace

    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        saved = []
        window = MainWindow(
            AppSettings(record_window_width=700, record_window_height=600),
            env.tasks,
            record_service=env.records,
            attachment_service=env.attachments,
            save_settings=saved.append,
        )
        qtbot.addWidget(window)
        dialog = window._show_task_records(env.tasks.get(item.task_id), initial_tab="attachments")
        assert dialog.width() == 700
        window._settings = replace(
            window._settings, automatic_backup_keep=7, hourly_notification_minute=42
        )
        dialog.resize(710, 610)
        dialog.reject()
        assert saved[-1].record_window_width == 710 and saved[-1].record_window_height == 610
        assert saved[-1].automatic_backup_keep == 7 and saved[-1].hourly_notification_minute == 42
        window.close()


def test_late_source_read_cannot_replace_new_selection_or_operation_notice(
    qtbot, tmp_path, monkeypatch
):
    from threading import Event

    with lifecycle(tmp_path) as (env, service):
        originals, works = mixed_files(env, service, tmp_path)
        release = Event()
        started = Event()
        lookup = service.catalog.source_summary

        def delayed(task_id, attachment_id):
            if attachment_id == works[0].id:
                started.set()
                release.wait(5)
            return lookup(task_id, attachment_id)

        monkeypatch.setattr(service.catalog, "source_summary", delayed)
        dialog = make_records(env, originals[0], tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.select_attachment(works[0].id)
        qtbot.waitUntil(started.is_set)
        dialog.select_attachment(works[1].id)
        dialog.attachment_detail.setText("저장 실패 · 안내 유지")
        release.set()
        qtbot.waitUntil(lambda: dialog._source_controller.result is not None)
        assert dialog.attachment_detail.full_text == "저장 실패 · 안내 유지"
        assert dialog._source_controller.result.current_name == originals[1].name
        dialog.shutdown()


def test_information_source_finishes_even_after_selection_changes(qtbot, tmp_path, monkeypatch):
    from threading import Event

    with lifecycle(tmp_path) as (env, service):
        originals, works = mixed_files(env, service, tmp_path)
        release, started = Event(), Event()
        lookup = service.catalog.source_summary

        def delayed(task_id, identity):
            if identity == works[0].id:
                started.set()
                release.wait(5)
            return lookup(task_id, identity)

        monkeypatch.setattr(service.catalog, "source_summary", delayed)
        dialog = make_records(env, originals[0], tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.select_attachment(works[0].id)
        qtbot.waitUntil(started.is_set)
        # The information read is queued behind another read, then selection changes again.
        dialog.select_attachment(works[1].id)
        dialog._show_attachment_information()
        information = dialog._source_info_dialog
        dialog.select_attachment(originals[2].id)
        release.set()
        edit = information.findChild(QPlainTextEdit)
        qtbot.waitUntil(lambda: "출처 확인 중" not in edit.toPlainText())
        assert "출처: " + originals[1].name in edit.toPlainText()
        assert "출처: " + originals[0].name not in edit.toPlainText()
        assert dialog._selected_attachment_ids() == (originals[2].id,)
        assert dialog.attachment_detail.full_text == "원본 · 내용 고정"
        dialog._show_attachment_information()
        assert dialog._source_info_dialog is information
        dialog.shutdown()


def test_source_reads_coalesce_and_shutdown_discards_late_results(qtbot, tmp_path):
    from threading import Event

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QWidget

    from officeflow.infrastructure.database.document_catalog import AttachmentSourceSummary
    from officeflow.presentation.attachment_browser import AttachmentSourceController

    owner = QWidget()
    qtbot.addWidget(owner)
    started, release = Event(), Event()
    calls, updates = [], []

    def delayed(task, identity):
        calls.append(identity)
        if len(calls) == 1:
            started.set()
            release.wait(5)
        return AttachmentSourceSummary("attachment", "live", str(identity))

    controller = AttachmentSourceController(delayed, owner)
    controller.updated.connect(lambda: updates.append(controller.result.current_name))
    controller.request(1, 1, 0, 0)
    qtbot.waitUntil(started.is_set)
    for identity in range(2, 101):
        controller.request(1, identity, 0, identity)
    release.set()
    qtbot.waitUntil(lambda: bool(updates))
    assert calls == [1, 100]
    assert updates == ["100"]
    controller.shutdown()
    assert controller.request(1, 101, 0, 101) is None

    started.clear()
    release.clear()
    calls.clear()
    updates.clear()
    closing = AttachmentSourceController(delayed, owner)
    closing.updated.connect(lambda: updates.append("closed"))
    closing.request(1, 1, 0, 0)
    qtbot.waitUntil(started.is_set)
    QTimer.singleShot(20, release.set)
    closing.shutdown()
    qtbot.wait(30)
    assert updates == []


def test_new_work_resets_kind_but_version_creation_and_role_change_do_not(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        entry = service.catalog.get("attachment", item.id)
        previous_work = service.create(entry, DocumentOperation.WORK, filename="previous.txt")
        dialog = make_records(env, item, tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.select_attachment(item.id)
        choose_kind(dialog, "original")
        service.create(
            service.catalog.get("attachment", previous_work.attachment_id),
            DocumentOperation.VERSION,
            label="제출 전",
        )
        dialog._refresh_attachments()
        assert dialog.attachment_kind_combo.currentData() == "original"
        created = service.create(entry, DocumentOperation.WORK, filename="draft.txt")
        dialog._refresh_attachments()
        assert dialog.attachment_kind_combo.currentData() == "all"
        assert dialog._selected_attachment_ids() == (item.id,)
        dialog.select_attachment(created.attachment_id)
        choose_kind(dialog, "work")
        service.create(
            service.catalog.get("attachment", created.attachment_id), DocumentOperation.ORIGINAL
        )
        dialog._refresh_attachments()
        assert dialog.attachment_kind_combo.currentData() == "work"
        assert dialog._selected_attachment_ids() == ()
        assert "현재 필터에서 제외" in dialog.attachment_detail.full_text
        dialog.shutdown()


def test_busy_filter_is_blocked_and_read_only_browsing_still_works(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, service):
        originals, works = mixed_files(env, service, tmp_path)
        dialog = make_records(env, originals[0], tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog._attachment_thread = object()
        choose_kind(dialog, "work")
        assert dialog.attachment_kind_combo.currentData() == "all"
        dialog._attachment_thread = None
        dialog._read_only = True
        dialog._apply_read_only_mode()
        choose_kind(dialog, "work")
        dialog.select_attachment(works[0].id)
        choose_kind(dialog, "work")
        assert not dialog.add_attachment_button.isEnabled()
        assert not dialog._attachment_metadata_enabled()
        assert dialog.open_attachment_button.isEnabled()
        dialog._show_attachment_information()
        assert dialog._source_info_dialog.isVisible() and not dialog._source_info_dialog.isModal()
        dialog.shutdown()


@pytest.mark.parametrize("total", [20, 100])
def test_many_small_attachments_keep_order_and_selection_when_view_changes(qtbot, tmp_path, total):
    from PySide6.QtCore import Qt

    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        path = tmp_path / "small.txt"
        for number in range(total - 1):
            path.write_bytes(f"small-{number}".encode())
            env.attachments.attach(item.task_id, path)
        dialog = make_records(env, item, tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        before = [
            dialog.attachment_list.item(n).data(Qt.ItemDataRole.UserRole) for n in range(total)
        ]
        dialog.select_attachment(before[-1])
        dialog._set_attachment_view("thumbnails")
        assert dialog._selected_attachment_ids() == (before[-1],)
        dialog._set_attachment_view("list")
        assert dialog._selected_attachment_ids() == (before[-1],)
        assert [
            dialog.attachment_list.item(n).data(Qt.ItemDataRole.UserRole) for n in range(total)
        ] == before
        assert dialog.attachment_list.verticalScrollBar().maximum() > 0
        dialog.shutdown()


def test_missing_family_uses_whole_task_scope_and_does_not_offer_related_filter(qtbot, tmp_path):
    from dataclasses import replace

    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        dialog = make_records(env, item, tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.select_attachment(item.id)
        dialog._attachments_by_id[item.id] = replace(item, family_id="")
        assert dialog._selected_attachment_family() is None
        dialog._open_document_browser()
        browser = next(
            box for box in file_actions(dialog, service).dialogs if isinstance(box, DocumentBrowser)
        )
        assert browser.family_id is None
        assert browser.scope_label.full_text == "이 업무 전체 자료"
        browser.shutdown()
        dialog.shutdown()


def test_small_work_area_clamps_geometry_and_uses_body_scroll(qtbot, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from PySide6.QtCore import QRect

    with lifecycle(tmp_path) as (env, service):
        item = original(env, service)
        dialog = make_records(env, item, tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        available = QRect(0, 0, 640, 360)
        monkeypatch.setattr(
            dialog, "screen", lambda: SimpleNamespace(availableGeometry=lambda: available)
        )
        dialog.setGeometry(dialog._fit_browser_geometry(QRect(1900, 1100, 1000, 900)))
        qtbot.wait(30)
        assert dialog.width() < available.width() and dialog.height() < available.height()
        assert (
            dialog.geometry().left() >= 0 and dialog.frameGeometry().bottom() <= available.bottom()
        )
        assert dialog.attachment_scroll.verticalScrollBar().maximum() > 0
        dialog.shutdown()


def test_window_size_save_failure_is_nonblocking(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, _service):

        def fail(_settings):
            raise OSError("settings unavailable")

        window = MainWindow(AppSettings(automatic_backup_keep=7), env.tasks, save_settings=fail)
        qtbot.addWidget(window)
        window._remember_record_window_size(710, 610)
        assert window._settings.record_window_width == 710
        assert window._settings.automatic_backup_keep == 7
        assert "설정은 저장하지 못했습니다" in window.statusBar().currentMessage()
        window.close()


def test_work_log_open_uses_latest_size_and_forwards_size_changes(qtbot, tmp_path):
    from officeflow.presentation.record_dialog import WorkLogBrowserDialog

    with lifecycle(tmp_path) as (env, _service):
        item = original(env, _service)
        remembered = [700, 600]
        browser = WorkLogBrowserDialog(
            task_service=env.tasks,
            record_service=env.records,
            attachment_service=env.attachments,
            record_window_size=lambda: tuple(remembered),
        )
        qtbot.addWidget(browser)
        notices = []
        browser.windowSizeChanged.connect(lambda w, h: notices.append((w, h)))
        browser._selected_task_context = (item.task_id, None)
        browser._open_selected_records()
        first = browser._record_dialogs[-1]
        assert first.width() == 700
        first.resize(710, 610)
        first.reject()
        assert notices == [(710, 610)]
        remembered[:] = notices[-1]
        browser._open_selected_records()
        second = browser._record_dialogs[-1]
        assert second.width() == 710 and second.height() == 610
        browser.shutdown()
