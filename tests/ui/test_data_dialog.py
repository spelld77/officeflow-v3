from __future__ import annotations

from pathlib import Path
from typing import cast

from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QFileDialog, QLineEdit, QMessageBox, QPushButton
from pytestqt.qtbot import QtBot

from officeflow.application.attachments import AttachmentService
from officeflow.application.exporting import ExportService
from officeflow.application.migration import (
    LegacyMigration,
    MigrationCounts,
    MigrationIssue,
    MigrationPreview,
    MigrationResult,
)
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
from officeflow.presentation.migration_dialog import LegacyMigrationDialog


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
        migration_service=cast(LegacyMigration, object()),
    )
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
        "전체 일정 ICS",
        "지금 백업",
        "백업에서 복원",
        "2.6 데이터 가져오기",
        "새로 고침",
        "닫기",
    } <= labels
    assert dialog.width() >= 500
    assert dialog.height() >= 480
    assert "현재 사용량" in dialog.usage_total_label.text()
    assert "휴지통" in dialog.usage_task_label.text()


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

    dialog.cleanup_list.setCurrentRow(0)
    assert dialog.restore_attachment_button.isEnabled()
    dialog._restore_cleanup_item()

    assert attachment_service.detached_attachments() == ()
    assert attachment_service.attachments_for_task(task.id)[0].id == attachment.id
    qtbot.waitUntil(lambda: dialog._thread is None)
    dialog.reject()
    engine.dispose()


def test_migration_dialog_shows_preview_and_has_keyboard_order(qtbot: QtBot) -> None:
    dialog = LegacyMigrationDialog(cast(LegacyMigration, object()))
    qtbot.addWidget(dialog)
    dialog.show()
    preview = MigrationPreview(
        source_database=Path("office_tasks.db"),
        source_sha256="a" * 64,
        attachment_root=None,
        source_counts=MigrationCounts(tasks=4),
        importable_counts=MigrationCounts(tasks=3, work_logs=2, notes=1, attachments=5),
        missing_attachments=1,
        orphan_work_logs=0,
        orphan_attachments=0,
        issues=(MigrationIssue("warning", "missing", "파일 1개 누락"),),
    )

    dialog._preview_succeeded(preview)

    assert "업무 3개" in dialog.summary_label.text()
    assert "파일 1개 누락" in dialog.issue_view.toPlainText()
    assert dialog.import_button.isEnabled()
    assert dialog.database_edit.nextInFocusChain().text() == "찾기"
    assert isinstance(dialog.findChild(QLineEdit), QLineEdit)


def test_migration_dialog_selects_default_paths_and_resets_preview(
    qtbot: QtBot, tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "office_tasks.db"
    source.write_bytes(b"db")
    saved_files = tmp_path / "saved_files"
    saved_files.mkdir()
    other = tmp_path / "other-files"
    other.mkdir()
    dialog = LegacyMigrationDialog(cast(LegacyMigration, object()))
    qtbot.addWidget(dialog)
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(source), ""),
    )
    monkeypatch.setattr(
        QFileDialog,
        "getExistingDirectory",
        lambda *_args, **_kwargs: str(other),
    )

    dialog._choose_database()
    assert dialog.database_edit.text() == str(source)
    assert dialog.attachment_edit.text() == str(saved_files)
    dialog._choose_attachment_root()
    assert dialog.attachment_edit.text() == str(other)

    dialog._preview = _preview()
    dialog.import_button.setEnabled(True)
    dialog.database_edit.setText(str(tmp_path / "changed.db"))
    assert dialog._preview is None
    assert not dialog.import_button.isEnabled()


def test_migration_dialog_handles_validation_and_completion_messages(
    qtbot: QtBot, tmp_path: Path, monkeypatch
) -> None:
    dialog = LegacyMigrationDialog(cast(LegacyMigration, object()))
    qtbot.addWidget(dialog)
    warnings: list[str] = []
    questions: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, _title, message: (
            questions.append(message) or QMessageBox.StandardButton.No
        ),
    )

    dialog._run_preview()
    assert "먼저 선택" in warnings[-1]

    blocked = MigrationPreview(
        source_database=tmp_path / "office_tasks.db",
        source_sha256="b" * 64,
        attachment_root=None,
        source_counts=MigrationCounts(tasks=1),
        importable_counts=MigrationCounts(),
        missing_attachments=0,
        orphan_work_logs=0,
        orphan_attachments=0,
        issues=(MigrationIssue("error", "blocked", "가져오기 불가"),),
    )
    dialog._preview_succeeded(blocked)
    assert not dialog.import_button.isEnabled()
    assert "오류가 있어" in warnings[-1]

    dialog._preview = _preview()
    dialog._run_import()
    assert questions
    assert dialog._thread is None

    result = MigrationResult(
        source_backup=tmp_path / "source.db",
        pending_restore=tmp_path / "pending.ofbackup",
        report_path=tmp_path / "report.json",
        imported_counts=MigrationCounts(tasks=3, work_logs=2, notes=1, attachments=5),
        skipped_tasks=0,
        missing_attachments=1,
    )
    dialog._import_succeeded(result)
    assert "준비 완료" in dialog.summary_label.text()
    assert not dialog.import_button.isEnabled()


def _preview() -> MigrationPreview:
    return MigrationPreview(
        source_database=Path("office_tasks.db"),
        source_sha256="a" * 64,
        attachment_root=None,
        source_counts=MigrationCounts(tasks=3),
        importable_counts=MigrationCounts(tasks=3),
        missing_attachments=0,
        orphan_work_logs=0,
        orphan_attachments=0,
        issues=(),
    )


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
