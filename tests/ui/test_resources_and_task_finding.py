import os
from datetime import UTC, datetime
from threading import Event

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QTimer
from PySide6.QtWidgets import QMenu, QPushButton
from shiboken6 import isValid

import officeflow.presentation.main_window as main_module
from officeflow.application.recent_tasks import RecentTaskRef, RecentTasks
from officeflow.application.records import RecordService
from officeflow.application.resource_policy import ResourceState
from officeflow.application.tasks import TaskDraft, TaskQuery, TaskService, TaskView
from officeflow.infrastructure.database.completed_browse_repository import (
    SqlAlchemyCompletedBrowseRepository,
)
from officeflow.infrastructure.database.recent_task_repository import SqlAlchemyRecentTaskRepository
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.completed_browse import CompletedBrowseWidget
from officeflow.presentation.latest_job import LatestJob
from officeflow.presentation.main_window import MainWindow
from officeflow.presentation.record_dialog import CompleteTaskDialog, TaskRecordsDialog
from officeflow.presentation.reminder_dialog import ReminderDialog
from officeflow.presentation.resource_controller import ResourceController
from tests.unit.test_record_service import InMemoryRecordRepository
from tests.unit.test_task_service import InMemoryTaskRepository


def drain(qapp):
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def test_menu_completion_and_reminder_native_objects_return_to_baseline(qtbot, qapp, monkeypatch):
    service = TaskService(InMemoryTaskRepository())
    task = service.create(TaskDraft(title="합성 자원 검사"))
    window = MainWindow(AppSettings(), service)
    qtbot.addWidget(window)
    window.show()
    window._set_view(TaskView.ALL)
    window._task_list.setCurrentIndex(window._task_model.index_for_task(task.id))
    baseline = len(window.findChildren(QMenu))

    class ClosedMenu(QMenu):
        def exec(self, *_args):
            return None

    def build(_task):
        menu = (
            QMenu(window) if os.environ.get("QT_QPA_PLATFORM") == "windows" else ClosedMenu(window)
        )
        menu.addAction("검사")
        if os.environ.get("QT_QPA_PLATFORM") == "windows":
            QTimer.singleShot(15, menu.close)
        return menu

    monkeypatch.setattr(window, "_build_task_context_menu", build)
    print("resource lifecycle: menus", flush=True)
    for _ in range(100):
        window._show_task_menu_for_task(task, window.mapToGlobal(QPoint(100, 100)))
        drain(qapp)
    assert len(window.findChildren(QMenu)) == baseline

    class ClosingCompletion(CompleteTaskDialog):
        def exec(self):
            QTimer.singleShot(15, self.reject)
            return super().exec()

    monkeypatch.setattr(main_module, "CompleteTaskDialog", ClosingCompletion)
    print("resource lifecycle: completion dialogs", flush=True)
    for _ in range(40):
        window._complete_selected_with_result()
        drain(qapp)
    assert len(window.findChildren(CompleteTaskDialog)) == 0
    print("resource lifecycle: reminders", flush=True)
    for _ in range(100):
        popup = ReminderDialog((), timezone="Asia/Seoul")
        popup.finished.connect(
            lambda _result, current=popup: window._clear_reminder_dialog(current)
        )
        window._reminder_dialog = popup
        popup.show()
        popup.accept()
        drain(qapp)
        assert not isValid(popup)
    assert window._reminder_dialog is None
    assert service.get(task.id).status.value == "active"


def test_old_popup_finished_does_not_clear_new_popup(qtbot, qapp):
    window = MainWindow(AppSettings(), TaskService(InMemoryTaskRepository()))
    qtbot.addWidget(window)
    old, new = (ReminderDialog((), timezone="Asia/Seoul") for _ in range(2))
    window._reminder_dialog = new
    window._clear_reminder_dialog(old)
    drain(qapp)
    assert window._reminder_dialog is new and isValid(new)
    window._clear_reminder_dialog(new)
    drain(qapp)


def test_latest_worker_coalesces_and_rejects_stale_results(qtbot):
    reader = LatestJob()
    started, release = Event(), Event()
    calls, results = [], []

    def first():
        calls.append(0)
        started.set()
        assert release.wait(5)
        return 0

    reader.ready.connect(results.append)
    reader.submit(first)
    qtbot.waitUntil(started.is_set)
    for i in range(1, 100):
        reader.submit(lambda i=i: calls.append(i) or i)
    release.set()
    qtbot.waitUntil(lambda: results == [99])
    assert calls == [0, 99]
    reader.shutdown()


def test_hidden_main_poll_does_not_read_task_lists(qtbot, monkeypatch):
    repo = InMemoryTaskRepository()
    window = MainWindow(AppSettings(), TaskService(repo))
    qtbot.addWidget(window)
    window.show()
    window.hide()
    window._desktop_integration = True
    calls = []
    monkeypatch.setattr(window, "_request_reminder_preview", lambda: calls.append("preview"))
    before = len(repo.queries)
    for _ in range(40):
        window._check_reminders()
        window._refresh_tasks()
    assert len(repo.queries) == before and calls == [] and window._view_dirty
    window.show()
    window._refresh_time_sensitive_view()
    assert len(repo.queries) > before and not window._view_dirty


def test_resource_controller_samples_numeric_bounded_and_closes(qtbot, tmp_path):
    controller = ResourceController(tmp_path)
    qtbot.waitUntil(lambda: bool(controller.samples))
    for i in range(24):
        controller.request()
        qtbot.waitUntil(lambda i=i: len(controller.samples) >= min(16, i + 1))
    assert len(controller.samples) <= 16
    controller.shutdown()
    assert controller._sampler.probe is not None
    assert controller._sampler.probe._handle is None
    assert controller._jobs._thread is None
    payload = (tmp_path / "runtime-resources.log").read_text()
    assert '"private_bytes"' in payload and '"event": "shutdown"' in payload
    assert "description" not in payload and "task_id" not in payload


def test_recent_entry_reuses_drafts_without_changing_search_or_task(qtbot, task_service):
    task = task_service.create(TaskDraft(title="최근 업무"))
    sessions = task_service._repository._sessions
    recent = RecentTasks(SqlAlchemyRecentTaskRepository(sessions))
    window = MainWindow(
        AppSettings(),
        task_service,
        record_service=RecordService(InMemoryRecordRepository(), task_service),
        recent_tasks=recent,
    )
    qtbot.addWidget(window)
    window.show()
    window._search.setText("필터 유지")
    before = task_service.get(task.id)
    first = window._show_task_records(task)
    assert first is not None
    ref = recent.entries[0]
    window._open_recent_task(ref)
    assert window._show_task_records(task) is first
    assert window._search.text() == "필터 유지"
    assert task_service.get(task.id) == before
    assert len(window.findChildren(TaskRecordsDialog)) == 1


def test_recent_menu_more_closes_and_is_bounded(qtbot, qapp, task_service):
    sessions = task_service._repository._sessions
    recent = RecentTasks(SqlAlchemyRecentTaskRepository(sessions))
    for i in range(21):
        task = task_service.create(TaskDraft(title="긴 제목 " * 10 + str(i), is_pinned=i < 4))
        recent.record_viewed(RecentTaskRef(task.id, task.created_at, None, datetime.now(UTC)))
    window = MainWindow(AppSettings(), task_service, recent_tasks=recent)
    qtbot.addWidget(window)
    window.show()
    controller = window._recent_controller
    assert controller is not None
    for _ in range(100):
        controller.show_menu()
        qtbot.waitUntil(lambda: bool(controller._resolved) and not controller._reader._active)
        controller.menu.close()
        drain(qapp)
    assert len(recent.entries) == 20
    assert len(controller.menu.findChildren(QPushButton)) <= 8
    for _ in range(40):
        controller.show_more()
        dialog = controller.more
        qtbot.waitUntil(
            lambda: controller._listing is not None and controller._listing.count() == 20
        )
        dialog.reject()
        drain(qapp)
        assert not isValid(dialog)
    assert controller.more is None


def test_monthly_groups_pages_and_selection_keys(qtbot, task_service):
    now = datetime(2026, 10, 20, 3, tzinfo=UTC)
    from officeflow.domain.enums import TaskStatus

    for i in range(52):
        task_service.create(TaskDraft(title=f"완료 {i}", status=TaskStatus.COMPLETED), now=now)
    for month in (9, 8):
        task_service.create(
            TaskDraft(title=f"{month}월", status=TaskStatus.COMPLETED), now=now.replace(month=month)
        )
    widget = CompletedBrowseWidget(
        SqlAlchemyCompletedBrowseRepository(task_service._repository._sessions)
    )
    qtbot.addWidget(widget)
    widget.resize(560, 430)
    widget.show()
    widget.refresh(TaskQuery(view=TaskView.COMPLETED), "Asia/Seoul")
    qtbot.waitUntil(lambda: widget.result is not None)
    assert widget.result.total == 54 and len(widget.result.pages[0].entries) == 50
    widget._next_page((2026, 10), widget.result.pages[0].next_cursor)
    qtbot.waitUntil(lambda: len(widget.result.pages[0].entries) == 2)
    widget._previous_page((2026, 10))
    qtbot.waitUntil(lambda: len(widget.result.pages[0].entries) == 50)
    widget.tree.topLevelItem(1).setExpanded(True)
    qtbot.waitUntil(lambda: len(widget.result.pages) == 2)
    widget.tree.topLevelItem(2).setExpanded(True)
    qtbot.waitUntil(lambda: len(widget.result.pages) == 2 and (2026, 8) in widget._expanded)
    assert sum(len(page.entries) for page in widget.result.pages) <= 100
    assert (2026, 10) not in widget._expanded
    widget.shutdown()


def test_memory_pressure_defers_only_new_automatic_backup(qtbot, monkeypatch):
    from unittest.mock import Mock

    manager = Mock()
    manager.should_create_automatic_backup.return_value = True
    window = MainWindow(AppSettings(), TaskService(InMemoryTaskRepository()))
    qtbot.addWidget(window)
    window._backup_manager = manager
    monkeypatch.setattr(window, "_refresh_backup_health", lambda: None)
    window._resource_state_changed(ResourceState.PRESSURE)
    window._maybe_automatic_backup()
    assert window._backup_waiting_for_memory
    manager.create_backup.assert_not_called()
    manager.health.begin.assert_not_called()
    window._backup_manager = None


def test_record_metadata_and_progress_are_released(qtbot, qapp, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from PySide6.QtWidgets import QProgressDialog

    import officeflow.presentation.record_dialog as record_module
    from officeflow.presentation.attachment_metadata import AttachmentMetadataDialog

    service = TaskService(InMemoryTaskRepository())
    task = service.create(TaskDraft(title="synthetic"))
    dialog = TaskRecordsDialog(
        task,
        task_service=service,
        record_service=RecordService(InMemoryRecordRepository(), service),
    )
    qtbot.addWidget(dialog)
    dialog.show()
    attachment_service = Mock()
    attachment_service.get_metadata.return_value = SimpleNamespace(
        name="synthetic.txt", memo="", metadata_revision=1, id_required=1
    )
    dialog._attachment_service = attachment_service
    dialog._selected_attachment_id = 1
    monkeypatch.setattr(dialog, "_attachment_metadata_enabled", lambda: True)
    monkeypatch.setattr(dialog, "_refresh_pending_attachments", lambda: None)

    class ClosingMetadata(AttachmentMetadataDialog):
        def exec(self):
            QTimer.singleShot(5, self.reject)
            return super().exec()

    monkeypatch.setattr(record_module, "AttachmentMetadataDialog", ClosingMetadata)
    for i in range(80):
        dialog._edit_attachment_metadata(edit_memo=i % 2 == 0)
        drain(qapp)
    assert not dialog.findChildren(AttachmentMetadataDialog)
    for _ in range(40):
        progress = QProgressDialog("synthetic", "cancel", 0, 1, dialog)
        progress.canceled.connect(dialog._attachment_import_was_canceled)
        dialog._attachment_progress = progress
        dialog._attachment_import_finished()
        drain(qapp)
        assert not isValid(progress)
        assert not dialog._attachment_import_canceled
    dialog._attachment_service = None


def test_export_option_dialogs_are_released_on_cancel(qtbot, qapp, monkeypatch, tmp_path):
    import officeflow.presentation.data_dialog as data_module
    from officeflow.bootstrap.paths import AppPaths
    from officeflow.infrastructure.backup import BackupManager
    from officeflow.infrastructure.database.migrate import upgrade_database
    from officeflow.presentation.calendar_export_dialog import CalendarExportDialog
    from officeflow.presentation.data_dialog import DataManagementDialog
    from officeflow.presentation.task_archive_dialog import TaskArchiveDialog

    paths = AppPaths(tmp_path / "synthetic")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    monkeypatch.setattr(DataManagementDialog, "_refresh_usage", lambda _self: None)
    dialog = DataManagementDialog(
        export_service=object(),
        backup_manager=BackupManager(paths),
        query=TaskQuery(),
        task_archive_service=object(),
    )
    qtbot.addWidget(dialog)

    class ClosingCalendar(CalendarExportDialog):
        def exec(self):
            QTimer.singleShot(5, self.reject)
            return super().exec()

    class ClosingArchive(TaskArchiveDialog):
        def exec(self):
            QTimer.singleShot(5, self.reject)
            return super().exec()

    monkeypatch.setattr(data_module, "CalendarExportDialog", ClosingCalendar)
    monkeypatch.setattr(data_module, "TaskArchiveDialog", ClosingArchive)
    for _ in range(40):
        dialog._export_ics()
        dialog._export_task_archive()
        drain(qapp)
    assert not dialog.findChildren(CalendarExportDialog)
    assert not dialog.findChildren(TaskArchiveDialog)


def test_resource_log_rotation_and_failed_probe_are_nonfatal(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from officeflow.presentation.resource_controller import _Sampler

    sampler = _Sampler(tmp_path)
    sampler.sample({"hidden": False})
    assert sampler.handler is not None
    sampler.handler.maxBytes = 500
    for _ in range(50):
        sampler.write({"numeric": 123456})
    assert len(list(tmp_path.glob("runtime-resources.log*"))) == 3
    assert all(path.stat().st_size < 1024 for path in tmp_path.glob("runtime-resources.log*"))
    sampler.probe.close()
    sampler.probe = Mock()
    sampler.probe.sample.side_effect = OSError("synthetic")
    snapshot, _state = sampler.sample({})
    assert snapshot.commit_bytes is None
    sampler.close()


def test_native_resource_counts_plateau(qtbot, qapp):
    from officeflow.infrastructure.windows.resources import WindowsResourceProbe

    if os.environ.get("QT_QPA_PLATFORM") != "windows":
        pytest.skip("separate native Windows run")
    probe = WindowsResourceProbe()
    observations = []
    for block in range(4):
        for _ in range(40):
            popup = ReminderDialog((), timezone="Asia/Seoul")
            popup.show()
            qapp.processEvents()
            popup.accept()
            popup.deleteLater()
            drain(qapp)
        snapshot = probe.sample()
        observations.append(snapshot)
        print(
            f"native resource block={block} private={snapshot.private_bytes} handles={snapshot.handles} gdi={snapshot.gdi} user={snapshot.user}",
            flush=True,
        )
    probe.close()
    for field in ("gdi", "user", "handles"):
        values = [getattr(sample, field) for sample in observations[1:]]
        assert all(isinstance(value, int) for value in values)
        assert max(values) - min(values) <= 6


@pytest.mark.parametrize("dark", [False, True])
def test_application_task_finding_at_small_size_and_injected_palette(
    qtbot, qapp, monkeypatch, tmp_path, dark
):
    from pathlib import Path

    from PySide6.QtGui import QColor, QPalette

    from officeflow.bootstrap.paths import AppPaths
    from officeflow.domain.enums import TaskStatus
    from officeflow.infrastructure.settings.store import JsonSettingsStore
    from officeflow.main import build_application

    paths = AppPaths(tmp_path / "synthetic-profile")
    paths.ensure_directories()
    JsonSettingsStore(paths.settings_file).save(AppSettings(automatic_backup_enabled=False))
    monkeypatch.setenv("OFFICEFLOW_DATA_DIR", str(paths.root))
    previous = qapp.palette()
    if dark:
        palette = QPalette(previous)
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#202020"))
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
        ):
            palette.setColor(role, QColor("#eeeeee"))
        qapp.setPalette(palette)
    _app, window = build_application(application=qapp)
    qtbot.addWidget(window)
    try:
        window.resize(760, 560)
        window.show()
        tasks = []
        for i in range(12):
            task = window._task_service.create(
                TaskDraft(
                    title=f"합성 업무 {i} · 긴 제목 가독성 확인",
                    is_pinned=i < 4,
                    status=TaskStatus.COMPLETED if i % 2 == 0 else TaskStatus.ACTIVE,
                )
            )
            tasks.append(task)
            window._record_recent_view(task, None)
        window._set_view(TaskView.COMPLETED)
        browser = window._completed_browser
        qtbot.waitUntil(lambda: browser.result is not None)
        assert browser.result.total == 6
        assert browser.tree.viewport().palette().color(QPalette.ColorRole.Base).lightness() > 200
        assert browser.tree.palette().color(QPalette.ColorRole.Text).lightness() < 150
        for button in (browser.older, browser.recent, window._recent_button):
            assert window.rect().contains(button.mapTo(window, QPoint(0, 0)))
            assert button.height() >= button.fontMetrics().height() + 6
        controller = window._recent_controller
        qtbot.waitUntil(lambda: window._recent_button.isEnabled())
        controller.show_menu()
        qtbot.waitUntil(lambda: bool(controller._resolved) and not controller._reader._active)
        assert controller.menu.geometry().width() <= window.screen().availableGeometry().width()
        assert controller.menu.geometry().height() <= window.screen().availableGeometry().height()
        directory = os.environ.get("OFFICEFLOW_QA_CAPTURE_DIR")
        if directory:
            destination = Path(directory).resolve()
            root = Path(__file__).resolve().parents[2] / "artifacts"
            assert destination.is_relative_to(root)
            destination.mkdir(parents=True, exist_ok=True)
            name = os.environ.get("QT_SCALE_FACTOR", "1") + ("-dark-injected" if dark else "-light")
            assert window.grab().save(str(destination / f"completed-{name}.png"))
            assert controller.menu.grab().save(str(destination / f"recent-{name}.png"))
        controller.menu.close()
        first = window._show_task_records(tasks[0])
        assert window._show_task_records(tasks[0]) is first
    finally:
        window.shutdown()
        qapp.setPalette(previous)


def test_restore_invalidates_local_recent_history_and_backup_excludes_it(
    qtbot, qapp, monkeypatch, tmp_path
):
    import zipfile

    from officeflow.bootstrap.paths import AppPaths
    from officeflow.infrastructure.backup import BackupManager
    from officeflow.infrastructure.database.migrate import upgrade_database
    from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
    from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository
    from officeflow.infrastructure.recent_tasks import RecentTaskStore
    from officeflow.infrastructure.settings.store import JsonSettingsStore
    from officeflow.main import build_application

    paths = AppPaths(tmp_path / "synthetic-restore")
    paths.ensure_directories()
    JsonSettingsStore(paths.settings_file).save(AppSettings(automatic_backup_enabled=False))
    upgrade_database(paths.database_file)
    engine = create_database_engine(paths.database_file)
    service = TaskService(SqlAlchemyTaskRepository(SessionFactory(engine)))
    task = service.create(TaskDraft(title="synthetic retained business data"))
    ref = RecentTaskRef(task.id, task.created_at, None, datetime.now(UTC))
    store = RecentTaskStore(paths.recent_tasks_file, paths.database_file)
    store.save((ref,), store.reserve())
    manager = BackupManager(paths)
    backup = manager.create_backup()
    with zipfile.ZipFile(backup.path) as archive:
        assert all("recent-tasks.json" not in name for name in archive.namelist())
    engine.dispose()
    manager.stage_restore(backup.path)
    monkeypatch.setenv("OFFICEFLOW_DATA_DIR", str(paths.root))
    _app, window = build_application(application=qapp)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window._recent_button.isEnabled())
    assert window._recent_tasks.entries == ()
    assert window._task_service.get(task.id).title == task.title
    window.shutdown()


def test_recent_clear_waits_out_old_writer_and_flushes_empty(qtbot, task_service, tmp_path):
    from officeflow.infrastructure.recent_tasks import RecentTaskStore
    from officeflow.presentation.recent_tasks import RecentTaskController

    path = tmp_path / "recent.json"
    database = tmp_path / "db"
    database.write_bytes(b"synthetic identity")
    store = RecentTaskStore(path, database)
    service = RecentTasks(SqlAlchemyRecentTaskRepository(task_service._repository._sessions))
    button = QPushButton("recent")
    qtbot.addWidget(button)
    controller = RecentTaskController(service, button, timezone="Asia/Seoul", store=store)
    qtbot.waitUntil(button.isEnabled)
    task = task_service.create(TaskDraft(title="synthetic"))
    controller.record(task.id, task.created_at, None, datetime.now(UTC))
    controller._save()
    controller.clear()
    controller.shutdown()
    assert store.load() == ()


def test_recent_invalid_occurrence_does_not_guess_another(qtbot, task_service, monkeypatch):
    from datetime import timedelta

    from PySide6.QtWidgets import QMessageBox

    now = datetime(2026, 10, 26, 3, tzinfo=UTC)
    task = task_service.create(
        TaskDraft(title="synthetic daily", starts_at=now, recurrence_rule="FREQ=DAILY")
    )
    service = RecentTasks(SqlAlchemyRecentTaskRepository(task_service._repository._sessions))
    records = RecordService(InMemoryRecordRepository(), task_service)
    window = MainWindow(AppSettings(), task_service, record_service=records, recent_tasks=service)
    qtbot.addWidget(window)
    window.show()
    invalid = RecentTaskRef(task.id, task.created_at, now + timedelta(hours=2), now)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)
    window._open_recent_task(invalid)
    assert not window.findChildren(TaskRecordsDialog)
    assert not service.entries
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    window._open_recent_task(invalid)
    assert service.entries[0].occurrence_start is None


def test_recent_clear_discards_late_read(qtbot, task_service):
    from officeflow.presentation.recent_tasks import RecentTaskController

    service = RecentTasks(SqlAlchemyRecentTaskRepository(task_service._repository._sessions))
    task = task_service.create(TaskDraft(title="synthetic"))
    service.record_viewed(RecentTaskRef(task.id, task.created_at, None, datetime.now(UTC)))
    button = QPushButton("recent")
    qtbot.addWidget(button)
    controller = RecentTaskController(service, button, timezone="Asia/Seoul")
    started, release = Event(), Event()
    original = service.resolve

    def delayed(refs):
        started.set()
        assert release.wait(5)
        return original(refs)

    service.resolve = delayed
    controller.show_more()
    qtbot.waitUntil(started.is_set)
    controller.clear()
    release.set()
    qtbot.waitUntil(lambda: not controller._reader._active)
    assert not service.entries and not controller._resolved
    assert controller._listing.count() == 0
    controller.shutdown()


def test_dirty_file_search_refreshes_on_return(qtbot):
    from unittest.mock import Mock

    window = MainWindow(AppSettings(), TaskService(InMemoryTaskRepository()))
    qtbot.addWidget(window)
    window.show()
    window._file_page = Mock()
    window._file_search_active = True
    window._view_dirty = True
    window._refresh_time_sensitive_view()
    window._file_page.refresh.assert_called_once()
    assert not window._view_dirty
    window._file_page = None
    window._file_search_active = False
