from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QListWidget, QPushButton
from pytestqt.qtbot import QtBot

from officeflow.application.records import RecordService
from officeflow.application.tasks import TaskDraft, TaskService
from officeflow.presentation.record_dialog import TaskRecordsDialog, WorkLogBrowserDialog
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
