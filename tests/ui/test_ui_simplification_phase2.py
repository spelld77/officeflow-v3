"""U22-8..14: concise defaults without hiding safety or changing data policy."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QFileDialog, QLabel, QPushButton

from officeflow.application.attachment_export import (
    AttachmentExportService,
    ExportBatchResult,
    ExportItemResult,
    ExportStatus,
    FileReference,
)
from officeflow.application.exporting import ExportService
from officeflow.application.folder_links import FolderLink, FolderLinkService
from officeflow.application.reminder_preview import ReminderPreviewItem, ReminderPreviewResult
from officeflow.application.task_archive import ArchiveScope
from officeflow.application.tasks import TaskDraft, TaskQuery, TaskService
from officeflow.bootstrap.paths import AppPaths
from officeflow.domain.enums import TaskStatus
from officeflow.domain.hourly_notification import HourlySettings
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.backup_files import BackupFilePage
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.backup_files_dialog import BackupFilesDialog
from officeflow.presentation.data_dialog import DataManagementDialog
from officeflow.presentation.folder_links import FolderLinksPanel
from officeflow.presentation.hourly_notification_settings import HourlyNotificationSettingsDialog
from officeflow.presentation.main_window import MainWindow
from officeflow.presentation.reminder_preview import ReminderPreviewWidget
from officeflow.presentation.settings_dialog import SettingsDialog
from officeflow.presentation.task_archive_dialog import TaskArchiveDialog
from tests.integration.test_document_lifecycle import lifecycle
from tests.integration.test_reliability_phase2 import one_backup
from tests.ui.test_hourly_notification_ui import FakeClock, controller
from tests.unit.test_task_service import InMemoryTaskRepository


def preview_item(identity=1, status="예정", *, at=None):
    task = TaskService(InMemoryTaskRepository()).create(TaskDraft("제출 준비"))
    when = at or datetime(2026, 10, 10, 6, tzinfo=UTC)
    return ReminderPreviewItem(task, identity, None, when, when, status)


def test_today_summary_keeps_pending_attention_and_partial_distinct(qtbot):
    widget = ReminderPreviewWidget("Asia/Seoul")
    qtbot.addWidget(widget)
    widget.show()
    now = datetime(2026, 10, 10, 5, tzinfo=UTC)
    items = (
        preview_item(),
        preview_item(2, "미확인", at=now - timedelta(minutes=10)),
        preview_item(3, "시각 지남 · 발생 기록 없음", at=now - timedelta(hours=1)),
    )
    widget.set_result(ReminderPreviewResult(items, True), now=now)
    assert widget.label.text() == "오늘 알림 총 3건 · 다음 15:00"
    assert "미확인 1건" in widget.attention.text()
    assert "발생 기록 확인 필요 1건" in widget.attention.text()
    assert not widget.hint.isVisible()
    widget.set_filter_active(True)
    assert widget.hint.isVisible()
    widget.set_result(ReminderPreviewResult(items, False), now=now)
    assert "일부 조회" in widget.label.text()
    assert "다음" not in widget.label.text()
    assert widget.more.text() == "더 조회…"


def test_general_settings_help_is_collapsed_and_retention_is_automatic(qtbot):
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)
    dialog.show()
    labels = [label.text() for label in dialog.findChildren(QLabel)]
    assert "자동 백업 보관 개수" in labels
    assert not dialog.settings_help.label.isVisible()
    dialog.settings_help.toggle_button.click()
    assert dialog.settings_hint.isVisible()
    assert "수동" in dialog.settings_hint.text()
    dialog.automatic_backup_check.setChecked(False)
    assert not dialog.backup_keep_spin.isEnabled()


def test_hourly_draft_is_not_current_runtime(qtbot):
    dialog = HourlyNotificationSettingsDialog(HourlySettings())
    qtbot.addWidget(dialog)
    dialog.show()
    assert not dialog.draft_label.isVisible()
    dialog.minute_spin.setValue(40)
    assert dialog.draft_label.isVisible()
    assert "저장 전" in dialog.draft_label.text()
    assert not dialog.behavior_help.label.isVisible()
    assert dialog.morning_check.text() == "평일 08시: 늦게 켜도 알림 받기"


def test_archive_only_shows_inputs_for_effective_scope(qtbot):
    dialog = TaskArchiveDialog((1, 2))
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.year.isVisible() and not dialog.start.isVisible()
    dialog.start.setDate(QDate(2026, 1, 3))
    dialog.scope.setCurrentIndex(1)
    assert dialog.start.isVisible() and not dialog.year.isVisible()
    dialog.scope.setCurrentIndex(2)
    assert not dialog.start.isVisible() and not dialog.completed.isVisible()
    assert "날짜/완료 조건 미적용" in dialog.scope_summary.text()
    dialog.scope.setCurrentIndex(1)
    assert dialog.start.date() == QDate(2026, 1, 3)
    assert not dialog.scope_details.label.isVisible()


def test_backup_single_page_hides_navigation_and_idle_cancel(qtbot, tmp_path):
    manager, archive = one_backup(tmp_path)
    dialog = BackupFilesDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    assert not dialog.cancel_button.isVisible()
    dialog.load(archive)
    assert dialog.cancel_button.isVisible()
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=10000)
    assert not dialog.previous_button.isVisible() and not dialog.next_button.isVisible()
    assert not dialog.cancel_button.isVisible()
    assert all(
        button.text() != "작업 취소"
        for button in dialog.findChildren(QPushButton)
        if button.isVisible()
    )
    dialog.reject()


@pytest.mark.parametrize("complete", [True, False])
def test_empty_and_closed_reminders_do_not_promise_unloaded_absence(qtbot, complete):
    widget = ReminderPreviewWidget("Asia/Seoul")
    qtbot.addWidget(widget)
    item = preview_item()
    closed = replace(item, task=replace(item.task, status=TaskStatus.COMPLETED))
    widget.set_result(
        ReminderPreviewResult((closed,), complete), now=item.effective_at - timedelta(minutes=1)
    )
    assert "다음" not in widget.label.text()
    assert ("남은 알림 없음" in widget.label.text()) == complete
    widget.set_result(ReminderPreviewResult((), complete))
    assert (widget.label.text() == "오늘 알림 없음") == complete


def test_preview_failure_survives_compact_change_and_retry(qtbot):
    widget = ReminderPreviewWidget("Asia/Seoul")
    qtbot.addWidget(widget)
    widget.set_error(ValueError("조회 실패 예시"))
    widget.set_compact(True)
    assert "조회 실패" in widget.label.text()
    requested = []
    widget.reloadRequested.connect(lambda: requested.append(True))
    widget._open_all()
    assert requested == [True]
    widget.set_result(ReminderPreviewResult((), True))
    assert widget.label.text() == "오늘 알림 없음"
    widget.shutdown()


class FolderListService:
    def list(self, task_id):
        return (
            FolderLink(
                1, task_id, "업무 자료", "D:/" + "긴 폴더/" * 40 + "최종자료", 0, datetime.now(UTC)
            ),
        )


def test_folder_menu_readonly_guards_and_complete_selectable_path(qtbot, monkeypatch):
    panel = FolderLinksPanel(cast(FolderLinkService, FolderListService()), 1, read_only=True)
    qtbot.addWidget(panel)
    panel.show()
    assert set(panel.buttons) == {"open", "add"}
    assert not panel.buttons["add"].isEnabled()
    assert not panel.menu_actions["delete"].isEnabled()
    assert panel.menu_actions["path"].isEnabled()
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *_: pytest.fail("readonly picker")
    )
    for action in (panel.add, panel.rename, panel.relink, panel.delete):
        action()
    panel._show_path()
    assert panel.selected.path in panel._path_dialog.text.toPlainText()
    assert panel.path.full_text.endswith("최종자료")
    panel._path_dialog.close()


def test_hourly_today_changes_and_save_do_not_overwrite_dates_or_runtime(qtbot):
    clock = FakeClock("2026-10-01T08:30")
    control = controller(qtbot, clock)
    try:
        control.open_settings()
        editor = control.settings_dialog
        editor.minute_spin.setValue(40)
        current = editor.next_label.text()
        assert "저장 전" in editor.draft_label.text()
        editor.holiday_check.click()
        assert control.service.settings.holiday_date == "2026-10-01"
        assert control.service.settings.minute == 35
        editor._save()
        assert control.service.settings.minute == 40
        assert control.service.settings.holiday_date == "2026-10-01"
        assert not editor.draft_label.isVisible()
        assert current != editor.next_label.text()
        assert "2E7D5B" in editor.error_label.styleSheet()
    finally:
        control.shutdown()


def test_hourly_save_failure_keeps_draft(qtbot):
    def failed(_settings):
        raise OSError("저장 실패 예시")

    control = controller(qtbot, FakeClock("2026-10-01T19:40"), save=failed)
    try:
        control.open_settings()
        editor = control.settings_dialog
        editor.minute_spin.setValue(42)
        editor._save()
        assert control.service.settings.minute == 35
        assert editor.draft_label.isVisible()
        assert "적용하지 못했습니다" in editor.error_label.text()
    finally:
        control.shutdown()


def test_extra_exclusions_initial_values_and_add_remove_draft(qtbot):
    settings = HourlySettings(extra_exclusions=(("23:00", "06:00"), ("12:00", "13:00")))
    dialog = HourlyNotificationSettingsDialog(settings)
    qtbot.addWidget(dialog)
    dialog.show()
    assert not dialog.draft_label.isVisible()
    dialog._add_range("15:00", "16:00")
    assert dialog.draft_label.isVisible()
    dialog._remove_range(dialog._ranges[-1][0])
    assert not dialog.draft_label.isVisible()


def test_data_warning_remains_after_acknowledgement_and_menus_guard_busy(qtbot, tmp_path):
    manager, _ = one_backup(tmp_path)
    manager.health.begin("automatic")
    manager.health.finish("automatic", "failed", "파일 접근 확인 필요")
    dialog = DataManagementDialog(
        export_service=cast(ExportService, object()), backup_manager=manager, query=TaskQuery()
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: "현황을 새로 확인했습니다" in dialog.status_label.text(), timeout=10000)
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=10000)
    dialog._acknowledge_backup_health()
    assert "실패" in dialog.backup_health_label.text()
    assert "안내 확인함" in dialog.backup_health_label.text()
    assert "파일 접근 확인 필요" in dialog.backup_health_details.label.text()
    assert not dialog.backup_health_details.label.isVisible()
    actions = [action.text() for action in dialog.backup_more_menu.actions()]
    assert "백업에서 파일 꺼내기…" in actions
    assert "오래된 수동 백업 정리…" in actions
    dialog._set_tool_actions_enabled(False)
    assert not any(action.isEnabled() for action in dialog._tool_menu_actions)
    dialog._set_tool_actions_enabled(True)
    assert all(action.isEnabled() for action in dialog._tool_menu_actions)
    dialog.shutdown()


def test_backup_result_mode_prevents_old_page_navigation_and_returns_to_first(qtbot, tmp_path):
    manager, archive = one_backup(tmp_path)
    dialog = BackupFilesDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.load(archive)
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=10000)
    hit = dialog.list.item(0).data(Qt.ItemDataRole.UserRole)
    dialog._received(BackupFilePage((hit,), True))
    dialog._buttons(False)
    assert dialog.next_button.isVisible() and dialog.next_button.isEnabled()
    dialog._page_cursors = [0, 100]
    dialog._received(BackupFilePage((hit,), False))
    dialog._buttons(False)
    assert dialog.previous_button.isEnabled() and not dialog.next_button.isEnabled()
    folder = tmp_path / "external"
    folder.mkdir()
    dialog.export((hit.id,), folder)
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=10000)
    assert dialog._showing_results
    assert not dialog.paging.isVisible()
    assert "실패 0" in dialog.status.text() and "미처리 0" in dialog.status.text()
    assert dialog.search_button.text() == "목록 다시 보기"
    dialog.previous()
    assert dialog._thread is None
    dialog.refresh()
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=10000)
    assert dialog._page_cursors == [0]
    assert not dialog._showing_results and not dialog.paging.isVisible()
    dialog.reject()


@pytest.mark.parametrize("close_method", ["shutdown", "reject", "accept"])
def test_data_immediate_close_does_not_start_deferred_worker(
    qtbot, tmp_path, monkeypatch, close_method
):
    manager, _ = one_backup(tmp_path)
    calls = []
    monkeypatch.setattr(manager, "inspect_data_usage", lambda **_: calls.append(True))
    dialog = DataManagementDialog(
        export_service=cast(ExportService, object()), backup_manager=manager, query=TaskQuery()
    )
    qtbot.addWidget(dialog)
    getattr(dialog, close_method)()
    qtbot.wait(50)
    assert dialog._thread is None and not calls


def test_archive_selected_ignores_hidden_invalid_dates(qtbot):
    dialog = TaskArchiveDialog((7, 9))
    qtbot.addWidget(dialog)
    dialog.scope.setCurrentIndex(1)
    dialog.start.setDate(QDate(2026, 12, 31))
    dialog.end.setDate(QDate(2026, 1, 1))
    with pytest.raises(ValueError):
        dialog.options()
    dialog.scope.setCurrentIndex(2)
    options = dialog.options()
    assert options.scope is ArchiveScope.SELECTED and options.task_ids == (7, 9)
    assert options.attachments and not options.versions and not options.snapshot_files
    dialog.scope.setCurrentIndex(1)
    assert dialog.start.date() == QDate(2026, 12, 31)


def test_main_preview_notice_only_tracks_real_search_and_filters(qtbot):
    window = MainWindow(AppSettings(), TaskService(InMemoryTaskRepository()))
    qtbot.addWidget(window)
    try:
        assert window._today_reminders.hint.isHidden()
        window._filter_toggle.click()
        assert window._today_reminders.hint.isHidden()
        window._search.setText("찾는 업무")
        assert not window._today_reminders.hint.isHidden()
        window._search.clear()
        assert window._today_reminders.hint.isHidden()
        window._pinned_filter.setChecked(True)
        assert not window._today_reminders.hint.isHidden()
        window._clear_filters_button.click()
        assert window._today_reminders.hint.isHidden()
    finally:
        window.shutdown()


@pytest.mark.parametrize("count,more", [(0, False), (100, False), (100, True)])
def test_backup_navigation_has_more_not_just_full_page(qtbot, tmp_path, count, more):
    manager, archive = one_backup(tmp_path)
    dialog = BackupFilesDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.load(archive)
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=10000)
    hit = dialog.list.item(0).data(Qt.ItemDataRole.UserRole)
    dialog._received(BackupFilePage(tuple(replace(hit, id=n + 1) for n in range(count)), more))
    dialog._buttons(False)
    assert dialog.paging.isVisible() == more
    dialog.list.selectAll()
    assert len(dialog.selected_ids()) == count
    dialog.shutdown()


def test_backup_partial_result_counts_and_full_detail_are_accessible(qtbot, tmp_path):
    manager, _ = one_backup(tmp_path)
    dialog = BackupFilesDialog(manager)
    qtbot.addWidget(dialog)
    reason = "한글 실패 원인 " * 50
    results = tuple(
        ExportItemResult(FileReference.attachment(n + 1), f"자료{n}.txt", status, message=reason)
        for n, status in enumerate(
            (ExportStatus.FAILED, ExportStatus.UNPROCESSED, ExportStatus.NEEDS_REVIEW)
        )
    )
    dialog._saved(ExportBatchResult("synthetic", results, tmp_path, canceled=True))
    dialog._buttons(False)
    assert all(text in dialog.status.text() for text in ("실패 1", "미처리 1", "확인 필요 1"))
    dialog._show_details()
    assert reason in dialog._detail_dialog.text.toPlainText()
    assert not dialog.folder_button.isVisible()
    dialog._detail_dialog.close()
    dialog.shutdown()


def test_data_recovery_menu_available_without_file_selection_and_no_auto_recovery(
    qtbot, tmp_path, monkeypatch
):
    paths = AppPaths(tmp_path / "profile")
    paths.ensure_directories()
    with lifecycle(paths.data_dir, attachment_root=paths.attachment_dir) as (env, _facade):
        exports = AttachmentExportService(env.attachments, ExternalExportStorage(paths.root))
        monkeypatch.setattr(
            exports, "inspect_recovery", lambda *_: pytest.fail("automatic recovery scan")
        )
        dialog = DataManagementDialog(
            export_service=cast(ExportService, object()),
            backup_manager=BackupManager(paths),
            query=TaskQuery(),
            attachment_service=env.attachments,
            attachment_export_service=exports,
        )
        qtbot.addWidget(dialog)
        dialog.show()
        qtbot.waitUntil(
            lambda: "현황을 새로 확인했습니다" in dialog.status_label.text(), timeout=10000
        )
        qtbot.waitUntil(lambda: dialog._thread is None, timeout=10000)
        dialog.tabs.setCurrentIndex(dialog._cleanup_tab_index)
        qtbot.waitUntil(lambda: dialog._thread is None and dialog._cleanup_loaded, timeout=10000)
        menu = dialog.cleanup_more_button.menu()
        menu.aboutToShow.emit()
        assert dialog.cleanup_list.currentItem() is None
        assert dialog.cleanup_more_button.isEnabled()
        assert all(action.isEnabled() for action in menu.actions()[2:] if not action.isSeparator())
        assert {"보기 사본 확인…", "중단된 내부 작업 확인…", "중단된 외부 저장 확인…"} <= {
            action.text() for action in menu.actions()
        }
        assert not menu.actions()[0].isEnabled() and not menu.actions()[1].isEnabled()
        dialog.shutdown()
