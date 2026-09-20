from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from threading import Event
from time import sleep

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QListWidget, QMessageBox, QPushButton
from pytestqt.qtbot import QtBot

from officeflow.application.attachments import AttachmentCanceledError, AttachmentService
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
    assert "월간 보고" in entries.item(0).text()
    assert "업무일지 2건" in entries.item(0).text()
    entries.setCurrentRow(0)
    assert "지표 확인" in dialog.detail.toPlainText()


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
    assert "완료 보고" in dialog.list_widget.item(0).text()
    assert "완료" in dialog.list_widget.item(0).text()
    assert "업무일지 0건" in dialog.list_widget.item(0).text()
    dialog.list_widget.setCurrentRow(0)
    assert "보고서 제출" in dialog.detail.toPlainText()
    assert "완료 요약" in dialog.detail.toPlainText()
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
    assert "한 달 전 계약 검토" in dialog.list_widget.item(0).text()
    assert "검색 위치: 설명" in dialog.list_widget.item(0).text()
    assert not dialog.date_edit.isEnabled()

    dialog.search_edit.setText("E501")
    dialog._refresh()

    assert dialog.list_widget.count() == 1
    assert "장애 분석" in dialog.list_widget.item(0).text()
    assert "검색 위치: 업무일지" in dialog.list_widget.item(0).text()

    dialog.search_edit.clear()
    dialog._refresh()
    assert dialog.date_edit.isEnabled()


def test_work_log_browser_pages_search_results_and_filters_date_range(
    qtbot: QtBot,
) -> None:
    task_service, record_service = make_dialog_services()
    for index in range(30):
        completed_at = datetime(2026, 8, index % 20 + 1, 4, 0, tzinfo=UTC)
        task = task_service.create(
            TaskDraft(title=f"누적 검색 완료 {index:02d}"),
            now=completed_at,
        )
        assert task.id is not None
        task_service.transition(task.id, TaskStatus.COMPLETED, now=completed_at)
        record_service.add_work_log(
            task_id=task.id,
            log_date=date(2026, 8, index % 20 + 1),
            content=f"누적 검색 일지 {index:02d}",
        )
    dialog = WorkLogBrowserDialog(
        task_service=task_service,
        record_service=record_service,
    )
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.search_edit.setText("누적 검색")
    dialog._refresh()

    assert 25 <= dialog.list_widget.count() <= 30
    assert dialog.load_more_button.isVisible()
    qtbot.mouseClick(dialog.load_more_button, Qt.MouseButton.LeftButton)
    assert dialog.list_widget.count() == 30
    assert not dialog.load_more_button.isVisible()

    dialog.range_checkbox.setChecked(True)
    dialog.range_from_edit.setDate(QDate(2026, 8, 10))
    dialog.range_to_edit.setDate(QDate(2026, 8, 12))
    dialog._refresh()

    assert dialog.list_widget.count() == 4
    assert dialog.result_count_label.text() == "검색 결과 4개 업무"


def test_work_log_browser_groups_completion_and_logs_by_task(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    task_service, record_service = make_dialog_services()
    completed_at = datetime(2026, 9, 15, 4, 0, tzinfo=UTC)
    task = task_service.create(
        TaskDraft(title="거래처 계약 검토", description="계약 갱신"),
        now=completed_at,
    )
    assert task.id is not None
    task_service.complete(task.id, result_note="수정본 전달 완료", now=completed_at)
    record_service.add_work_log(
        task_id=task.id,
        log_date=date(2026, 9, 14),
        content="계약서 초안 검토",
    )
    record_service.add_work_log(
        task_id=task.id,
        log_date=date(2026, 9, 15),
        content="수정본 전달",
        result="상대방 확인 대기",
    )
    attachment_service = AttachmentService(
        InMemoryAttachmentRepository(),
        ManagedAttachmentStorage(tmp_path / "attachments"),
        task_service,
    )
    source = tmp_path / "계약서.txt"
    source.write_text("contract", encoding="utf-8")
    attachment_service.attach(task.id, source)
    dialog = WorkLogBrowserDialog(
        task_service=task_service,
        record_service=record_service,
        attachment_service=attachment_service,
    )
    qtbot.addWidget(dialog)
    dialog.date_edit.setDate(QDate(2026, 9, 15))

    assert dialog.list_widget.count() == 1
    assert "업무일지 2건" in dialog.list_widget.item(0).text()
    dialog.list_widget.setCurrentRow(0)
    assert "수정본 전달 완료" in dialog.detail.toPlainText()
    assert "업무일지 2건" in dialog.detail.toPlainText()
    assert "진행 결과 / 다음 단계: 상대방 확인 대기" in dialog.detail.toPlainText()
    assert "첨부파일 1개" in dialog.detail.toPlainText()

    dialog.search_edit.setText("계약")
    dialog._refresh()

    assert dialog.list_widget.count() == 1
    assert dialog.result_count_label.text() == "검색 결과 1개 업무"
    assert "검색 위치:" in dialog.list_widget.item(0).text()


def test_task_records_dialog_confirms_exact_duplicate_work_log(
    qtbot: QtBot,
    monkeypatch,
) -> None:
    task_service, record_service = make_dialog_services()
    task = task_service.create(TaskDraft(title="중복 입력 확인"))
    assert task.id is not None
    record_service.add_work_log(
        task_id=task.id,
        log_date=date(2026, 9, 15),
        content="같은 내용",
    )
    questions: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, _title, message: (
            questions.append(message) or QMessageBox.StandardButton.Yes
        ),
    )
    dialog = TaskRecordsDialog(
        task,
        task_service=task_service,
        record_service=record_service,
    )
    qtbot.addWidget(dialog)
    dialog.log_date_edit.setDate(QDate(2026, 9, 15))
    dialog.log_content_edit.setPlainText("같은 내용")

    dialog._save_work_log()

    assert len(record_service.work_logs(task_id=task.id)) == 2
    assert questions and "그래도 새 기록으로 저장하시겠습니까?" in questions[0]


def test_record_dialog_distinguishes_completion_summary_from_progress_result(
    qtbot: QtBot,
) -> None:
    task_service, record_service = make_dialog_services()
    task = task_service.create(TaskDraft(title="용어 확인"))
    dialog = TaskRecordsDialog(
        task,
        task_service=task_service,
        record_service=record_service,
    )
    qtbot.addWidget(dialog)

    assert dialog.tabs.tabText(1) == "완료 요약"
    assert dialog.result_edit.placeholderText() == "완료 요약"
    assert dialog.log_result_edit.placeholderText() == "진행 결과, 이슈 또는 다음 단계"


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


def test_deleted_task_records_and_attachments_open_read_only(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    task_service, record_service = make_dialog_services()
    task = task_service.create(TaskDraft(title="휴지통 기록 조회"))
    assert task.id is not None
    record_service.add_checklist_item(task.id, "확인한 항목")
    record_service.add_work_log(
        task_id=task.id,
        log_date=date(2026, 9, 19),
        content="처리한 내용",
    )
    task_service.update_result_note(task.id, "처리 결과")
    attachment_service = AttachmentService(
        InMemoryAttachmentRepository(),
        ManagedAttachmentStorage(tmp_path / "attachments"),
        task_service,
    )
    source = tmp_path / "보존자료.txt"
    source.write_text("preserved", encoding="utf-8")
    attachment_service.attach(task.id, source)
    task_service.move_to_trash(task.id)

    deleted = task_service.get_including_deleted(task.id)
    dialog = TaskRecordsDialog(
        deleted,
        task_service=task_service,
        record_service=record_service,
        attachment_service=attachment_service,
    )
    qtbot.addWidget(dialog)

    assert dialog.checklist_list.count() == 1
    assert dialog.result_edit.toPlainText() == "처리 결과"
    assert dialog.work_log_list.count() == 1
    assert dialog.attachment_list.count() == 1
    assert dialog.result_edit.isReadOnly()
    assert dialog.add_attachment_button.isEnabled() is False
    dialog.attachment_list.setCurrentRow(0)
    assert dialog.open_attachment_button.isEnabled()
    assert dialog.unlink_attachment_button.isEnabled() is False
    assert dialog.delete_attachment_button.isEnabled() is False


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


def test_task_records_dialog_waits_for_attachment_cancel_before_closing(
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    task_service, record_service = make_dialog_services()
    task = task_service.create(TaskDraft(title="복사 중 닫기"))
    assert task.id is not None
    attachment_service = AttachmentService(
        InMemoryAttachmentRepository(),
        ManagedAttachmentStorage(tmp_path / "attachments"),
        task_service,
    )
    started = Event()

    def slow_attach(
        _task_id: int,
        _source: Path,
        *,
        cancel_requested=None,
        now=None,
    ) -> None:
        del now
        started.set()
        while cancel_requested is None or not cancel_requested():
            sleep(0.005)
        raise AttachmentCanceledError("사용자가 복사를 취소했습니다.")

    monkeypatch.setattr(attachment_service, "attach", slow_attach)
    source = tmp_path / "large.bin"
    source.write_bytes(b"copy")
    dialog = TaskRecordsDialog(
        task,
        task_service=task_service,
        record_service=record_service,
        attachment_service=attachment_service,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._start_attachment_import(source)
    qtbot.waitUntil(started.is_set, timeout=1_000)

    dialog.reject()

    assert dialog.isVisible()
    assert dialog._close_when_attachment_finishes
    qtbot.waitUntil(lambda: dialog._attachment_thread is None, timeout=3_000)
    qtbot.waitUntil(lambda: not dialog.isVisible(), timeout=1_000)


def test_work_log_browser_keeps_recurring_occurrence_context(qtbot: QtBot) -> None:
    task_service, record_service = make_dialog_services()
    occurrence_start = datetime(2026, 9, 15, 1, 0, tzinfo=UTC)
    task = task_service.create(
        TaskDraft(
            title="반복 점검",
            starts_at=occurrence_start,
            recurrence_rule="FREQ=DAILY;INTERVAL=1",
        )
    )
    assert task.id is not None
    work_log = record_service.add_work_log(
        task_id=task.id,
        occurrence_start=occurrence_start,
        log_date=date(2026, 9, 15),
        content="해당 발생 건 처리",
    )
    dialog = WorkLogBrowserDialog(
        task_service=task_service,
        record_service=record_service,
    )
    qtbot.addWidget(dialog)
    dialog.date_edit.setDate(QDate(2026, 9, 15))

    dialog.list_widget.setCurrentRow(0)

    assert work_log.occurrence_id is not None
    assert dialog._selected_task_context == (task.id, occurrence_start)
