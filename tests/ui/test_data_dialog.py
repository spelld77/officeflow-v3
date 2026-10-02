from __future__ import annotations

import os
from pathlib import Path
from typing import cast

from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import (
    QInputDialog,
    QMessageBox,
    QPushButton,
    QScrollArea,
)
from pytestqt.qtbot import QtBot

from officeflow.application.attachments import AttachmentService
from officeflow.application.exporting import ExportService
from officeflow.application.tasks import TaskDraft, TaskQuery, TaskService
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.attachments.storage import ManagedAttachmentStorage
from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.database.attachment_repository import (
    SqlAlchemyAttachmentRepository,
)
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository
from officeflow.presentation.data_dialog import DataManagementDialog, OperationWorker
from officeflow.presentation.theme import LIGHT_STYLESHEET


def test_data_dialog_exposes_usage_and_all_data_actions_at_minimum_size(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    paths = AppPaths(tmp_path / "officeflow")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    dialog = DataManagementDialog(
        export_service=cast(ExportService, object()),
        backup_manager=BackupManager(paths),
        query=TaskQuery(),
    )
    dialog.setStyleSheet(LIGHT_STYLESHEET)
    qtbot.addWidget(dialog)
    dialog.resize(480, 470)
    dialog.show()
    qtbot.waitUntil(
        lambda: "현재 사용량" in dialog.usage_total_label.text(),
        timeout=3_000,
    )

    labels = {button.text() for button in dialog.findChildren(QPushButton)}
    assert {
        "현재 목록 Excel",
        "일정 선택 ICS",
        "지금 백업",
        "백업에서 복원",
        "백업 폴더 열기",
        "오래된 수동 백업 정리",
        "새로 고침",
        "닫기",
    } <= labels
    assert "2.6 데이터 가져오기" not in labels
    assert dialog.width() >= 500
    assert dialog.height() >= 480
    assert "현재 사용량" in dialog.usage_total_label.text()
    assert "휴지통" in dialog.usage_task_label.text()
    assert "최대 5개" in dialog.usage_largest_title.text()
    assert "최근 자동 백업이 없습니다" in dialog.backup_schedule_label.text()
    assert "수동 0개" in dialog.backup_inventory_label.text()

    dialog.tabs.setCurrentIndex(dialog._tools_tab_index)
    tools_scroll = dialog.findChild(QScrollArea, "dataManagementScroll")
    assert tools_scroll is not None
    assert tools_scroll.verticalScrollBar().maximum() > 0
    dialog._refresh_usage()
    assert dialog.tabs.currentIndex() == dialog._tools_tab_index
    assert dialog.tabs.isTabEnabled(dialog._tools_tab_index)
    assert all(not button.isEnabled() for button in dialog._tool_action_buttons)
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=3_000)
    assert all(button.isEnabled() for button in dialog._tool_action_buttons)


def test_data_dialog_opens_managed_data_folders(
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = AppPaths(tmp_path / "officeflow")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    opened: list[str] = []
    monkeypatch.setattr(
        QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    dialog = DataManagementDialog(
        export_service=cast(ExportService, object()),
        backup_manager=BackupManager(paths),
        query=TaskQuery(),
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(
        lambda: dialog._thread is None and not dialog._usage_loading,
        timeout=3_000,
    )

    dialog._open_attachment_folder()
    dialog._open_backup_folder()

    assert [Path(path) for path in opened] == [
        paths.attachment_dir,
        paths.backup_dir,
    ]
    assert dialog.status_label.text() == "백업 폴더를 열었습니다."


def test_data_dialog_cleans_old_manual_backups_after_confirmation(
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = AppPaths(tmp_path / "officeflow")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    for index in range(4):
        backup = paths.backup_dir / f"officeflow-manual-20260920-12000{index}-test.ofbackup"
        backup.write_bytes(bytes(index + 1))
        backup.touch()
        os.utime(backup, (100 + index, 100 + index))
    monkeypatch.setattr(QInputDialog, "getInt", lambda *_args: (2, True))
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    manager = BackupManager(paths)
    dialog = DataManagementDialog(
        export_service=cast(ExportService, object()),
        backup_manager=manager,
        query=TaskQuery(),
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(
        lambda: dialog._thread is None and not dialog._usage_loading,
        timeout=3_000,
    )

    dialog._cleanup_manual_backups()

    qtbot.waitUntil(lambda: len(manager.list_manual_backups()) == 2, timeout=3_000)
    qtbot.waitUntil(
        lambda: dialog._thread is None and not dialog._usage_loading,
        timeout=3_000,
    )
    assert [path.name for path in manager.list_manual_backups()] == [
        "officeflow-manual-20260920-120003-test.ofbackup",
        "officeflow-manual-20260920-120002-test.ofbackup",
    ]


def test_data_dialog_restores_detached_attachment(qtbot: QtBot, tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "officeflow")
    paths.ensure_directories()
    upgrade_database(paths.database_file)
    engine = create_database_engine(paths.database_file)
    sessions = SessionFactory(engine)
    task_service = TaskService(SqlAlchemyTaskRepository(sessions))
    attachment_service = AttachmentService(
        SqlAlchemyAttachmentRepository(sessions),
        ManagedAttachmentStorage(paths.attachment_dir),
        task_service,
    )
    task = task_service.create(TaskDraft(title="복원할 업무"))
    assert task.id is not None
    source = tmp_path / "보고서.txt"
    source.write_text("content", encoding="utf-8")
    attachment = attachment_service.attach(task.id, source)
    attachment_service.unlink(attachment.id_required)
    dialog = DataManagementDialog(
        export_service=cast(ExportService, object()),
        backup_manager=BackupManager(paths),
        query=TaskQuery(),
        attachment_service=attachment_service,
        task_service=task_service,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog._cleanup_tab_index is not None
    dialog.tabs.setCurrentIndex(dialog._cleanup_tab_index)
    qtbot.waitUntil(
        lambda: dialog._cleanup_loaded and dialog.cleanup_list.count() > 0,
        timeout=3_000,
    )
    assert "보고서.txt" in dialog.cleanup_list.item(0).text()
    assert dialog.cleanup_list.item(0).text().startswith("복원 가능")

    dialog.cleanup_list.setCurrentRow(0)
    assert dialog.restore_attachment_button.isEnabled()
    assert "원래 업무로 복원" in dialog.cleanup_detail_label.text()
    dialog.cleanup_list.clearSelection()
    dialog.cleanup_list.setCurrentItem(None)
    assert "사용자가 직접 확인" in dialog.cleanup_detail_label.text()
    dialog.cleanup_list.setCurrentRow(0)
    dialog._restore_cleanup_item()

    assert attachment_service.detached_attachments() == ()
    assert attachment_service.attachments_for_task(task.id)[0].id == attachment.id
    qtbot.waitUntil(lambda: dialog._thread is None)
    dialog.reject()
    engine.dispose()


def test_operation_worker_reports_success_failure_and_cancel() -> None:
    success = OperationWorker(lambda canceled: "canceled" if canceled() else "done")
    success_spy = QSignalSpy(success.succeeded)
    success.run()
    assert success_spy.count() == 1
    assert success_spy.at(0)[0] == "done"

    canceled = OperationWorker(lambda is_canceled: is_canceled())
    canceled_spy = QSignalSpy(canceled.succeeded)
    canceled.cancel()
    canceled.run()
    assert canceled_spy.at(0)[0] is True

    failed = OperationWorker(lambda _canceled: (_ for _ in ()).throw(ValueError("failed")))
    failed_spy = QSignalSpy(failed.failed)
    failed.run()
    assert isinstance(failed_spy.at(0)[0], ValueError)
