from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QListWidget, QPushButton
from pytestqt.qtbot import QtBot

from officeflow.application.attachments import AttachmentService
from officeflow.application.records import RecordService
from officeflow.application.tasks import TaskDraft, TaskService
from officeflow.domain.enums import TaskStatus
from officeflow.infrastructure.attachments.storage import ManagedAttachmentStorage
from officeflow.infrastructure.database.attachment_repository import (
    SqlAlchemyAttachmentRepository,
)
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.record_repository import SqlAlchemyRecordRepository
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository
from officeflow.presentation.record_dialog import TaskRecordsDialog, WorkLogBrowserDialog
from tests.unit.test_attachment_service import InMemoryAttachmentRepository
from tests.unit.test_record_service import InMemoryRecordRepository
from tests.unit.test_task_service import InMemoryTaskRepository


def make_dialog_services() -> tuple[TaskService, RecordService]:
    task_service = TaskService(InMemoryTaskRepository())
    return task_service, RecordService(InMemoryRecordRepository(), task_service)


def test_task_records_dialog_adds_and_completes_checklist(qtbot: QtBot) -> None:
    task_service, record_service = make_dialog_services()
    task = task_service.create(TaskDraft(title="배포 준비"))
    assert task.id is not None
    dialog = TaskRecordsDialog(
        task,
        task_service=task_service,
        record_service=record_service,
    )
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.checklist_edit.setText("릴리스 노트 작성")
    qtbot.mouseClick(
        dialog.findChild(QPushButton, "addChecklistButton"),
        Qt.MouseButton.LeftButton,
    )
    assert dialog.checklist_list.count() == 1

    dialog.checklist_list.item(0).setCheckState(Qt.CheckState.Checked)
    saved = record_service.checklist_for_task(task.id)
    assert saved[0].is_done
    assert saved[0].completed_at is not None


def test_task_records_dialog_adds_and_edits_work_log(qtbot: QtBot) -> None:
    task_service, record_service = make_dialog_services()
    task = task_service.create(TaskDraft(title="회의"))
    assert task.id is not None
    dialog = TaskRecordsDialog(
        task,
        task_service=task_service,
        record_service=record_service,
    )
    qtbot.addWidget(dialog)
    dialog.tabs.setCurrentIndex(2)
    dialog.log_date_edit.setDate(QDate(2026, 9, 15))
    dialog.log_content_edit.setPlainText("설계 검토")
    dialog.log_result_edit.setPlainText("수정안 합의")

    qtbot.mouseClick(dialog.save_log_button, Qt.MouseButton.LeftButton)

    logs = record_service.work_logs(task_id=task.id)
    assert len(logs) == 1
    assert logs[0].result == "수정안 합의"
    assert dialog.work_log_list.count() == 1


def test_work_log_browser_filters_by_date(qtbot: QtBot) -> None:
    task_service, record_service = make_dialog_services()
    task = task_service.create(TaskDraft(title="월간 보고"))
    assert task.id is not None
    record_service.add_work_log(
        task_id=task.id,
        log_date=date(2026, 9, 15),
        content="지표 확인",
    )
    record_service.add_work_log(
        task_id=task.id,
        log_date=date(2026, 9, 16),
        content="보고 완료",
    )
    dialog = WorkLogBrowserDialog(
        task_service=task_service,
        record_service=record_service,
    )
    qtbot.addWidget(dialog)

    dialog.date_edit.setDate(QDate(2026, 9, 15))

    entries = dialog.findChild(QListWidget, "workLogBrowserList")
    assert entries is not None
    assert entries.count() == 1
    assert "지표 확인" in entries.item(0).text()


def test_work_log_browser_includes_tasks_completed_on_selected_date(qtbot: QtBot) -> None:
    task_service, record_service = make_dialog_services()
    completed_at = datetime(2026, 9, 15, 4, 0, tzinfo=UTC)
    task = task_service.create(TaskDraft(title="완료 보고"), now=completed_at)
    assert task.id is not None
    task_service.transition(task.id, TaskStatus.COMPLETED, now=completed_at)
    task_service.update_result_note(task.id, "보고서 제출", now=completed_at)
    dialog = WorkLogBrowserDialog(
        task_service=task_service,
        record_service=record_service,
    )
    qtbot.addWidget(dialog)

    dialog.date_edit.setDate(QDate(2026, 9, 15))

    assert dialog.list_widget.count() == 1
    assert "[완료] 완료 보고" in dialog.list_widget.item(0).text()
    assert "결과 있음" in dialog.list_widget.item(0).text()
    dialog.list_widget.setCurrentRow(0)
    assert "보고서 제출" in dialog.detail.text()
    assert dialog.open_records_button.isEnabled()


def test_work_log_browser_searches_past_tasks_and_log_content(qtbot: QtBot) -> None:
    task_service, record_service = make_dialog_services()
    completed_at = datetime(2026, 8, 15, 4, 0, tzinfo=UTC)
    completed = task_service.create(
        TaskDraft(title="한 달 전 계약 검토", description="갱신 조건 확인"),
        now=completed_at,
    )
    assert completed.id is not None
    task_service.transition(completed.id, TaskStatus.COMPLETED, now=completed_at)
    active = task_service.create(TaskDraft(title="장애 분석"))
    assert active.id is not None
    record_service.add_work_log(
        task_id=active.id,
        log_date=date(2026, 8, 20),
        content="오류코드 E501 원인 확인",
    )
    dialog = WorkLogBrowserDialog(
        task_service=task_service,
        record_service=record_service,
    )
    qtbot.addWidget(dialog)

    dialog.search_edit.setText("갱신 조건")
    dialog._refresh()

    assert dialog.list_widget.count() == 1
    assert "[완료 2026-08-15]" in dialog.list_widget.item(0).text()
    assert not dialog.date_edit.isEnabled()

    dialog.search_edit.setText("E501")
    dialog._refresh()

    assert dialog.list_widget.count() == 1
    assert "[일지 2026-08-20]" in dialog.list_widget.item(0).text()
    assert "오류코드 E501" in dialog.list_widget.item(0).text()

    dialog.search_edit.clear()
    dialog._refresh()
    assert dialog.date_edit.isEnabled()


def test_task_records_dialog_can_open_on_attachment_tab(qtbot: QtBot, tmp_path: Path) -> None:
    task_service, record_service = make_dialog_services()
    task = task_service.create(TaskDraft(title="첨부 바로가기"))
    attachment_service = AttachmentService(
        InMemoryAttachmentRepository(),
        ManagedAttachmentStorage(tmp_path / "attachments"),
        task_service,
    )
    dialog = TaskRecordsDialog(
        task,
        task_service=task_service,
        record_service=record_service,
        attachment_service=attachment_service,
        initial_tab="attachments",
    )
    qtbot.addWidget(dialog)

    assert dialog.tabs.tabText(dialog.tabs.currentIndex()) == "첨부파일"


def test_task_records_dialog_imports_attachment_without_blocking_ui(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    sessions = SessionFactory(engine)
    task_service = TaskService(SqlAlchemyTaskRepository(sessions))
    record_service = RecordService(SqlAlchemyRecordRepository(sessions), task_service)
    task = task_service.create(TaskDraft(title="계약 검토"))
    assert task.id is not None
    attachment_service = AttachmentService(
        SqlAlchemyAttachmentRepository(sessions),
        ManagedAttachmentStorage(tmp_path / "managed"),
        task_service,
    )
    source = tmp_path / "계약서.txt"
    source.write_text("contract", encoding="utf-8")
    dialog = TaskRecordsDialog(
        task,
        task_service=task_service,
        record_service=record_service,
        attachment_service=attachment_service,
    )
    qtbot.addWidget(dialog)
    dialog.tabs.setCurrentIndex(3)
    dialog.show()

    dialog._start_attachment_import(source)
    qtbot.waitUntil(lambda: dialog._attachment_thread is None, timeout=3_000)

    assert dialog.tabs.count() == 4
    assert dialog.attachment_list.count() == 1
    dialog.attachment_list.setCurrentRow(0)
    assert "SHA-256" in dialog.attachment_detail.text()
    engine.dispose()
