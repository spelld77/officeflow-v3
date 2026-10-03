from datetime import UTC, date, datetime, timedelta

import pytest
from PySide6.QtCore import QDate
from sqlalchemy import event

from officeflow.application.records import RecordService
from officeflow.application.tasks import TaskDraft, TaskService
from officeflow.domain.enums import OccurrenceStatus
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.models import AttachmentRecord
from officeflow.infrastructure.database.record_repository import SqlAlchemyRecordRepository
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository
from officeflow.presentation.record_dialog import WorkLogBrowserDialog


@pytest.fixture
def history(tmp_path):
    database = tmp_path / "history.db"
    upgrade_database(database)
    engine = create_database_engine(database)
    sessions = SessionFactory(engine)
    tasks = TaskService(SqlAlchemyTaskRepository(sessions))
    records = RecordService(SqlAlchemyRecordRepository(sessions), tasks)
    yield engine, tasks, records
    engine.dispose()


def _complete_series(tasks, count=3):
    start = datetime(2026, 9, 1, 1, tzinfo=UTC)
    task = tasks.create(
        TaskDraft(
            title="반복 점검",
            description="장비 유지",
            starts_at=start,
            recurrence_rule="FREQ=DAILY",
        ),
        now=start,
    )
    for number in range(count):
        tasks.transition_occurrence(
            task.id,
            start + timedelta(days=number),
            OccurrenceStatus.COMPLETED,
            now=start + timedelta(days=number, hours=1),
            result_note=f"검사결과{number:03} 이상없음",
        )
    return task, start


def test_recurring_title_description_result_search_and_sibling_isolation(history):
    _engine, tasks, records = history
    task, start = _complete_series(tasks)
    assert tasks.completed_search_page(search="반복").total == 3
    assert tasks.completed_search_page(search="장비").total == 3
    page = tasks.completed_search_page(search="검사결과001")
    assert page.total == 1
    assert page.items[0].occurrence_start == start + timedelta(days=1)
    assert page.items[0].result_note == "검사결과001 이상없음"
    records.add_work_log(
        task_id=task.id, occurrence_start=start, log_date=start.date(), content="유일한기록"
    )
    assert tasks.completed_search_page(search="유일한기록").total == 1
    assert tasks.completed_search_page(search='" OR *').total == 0
    assert tasks.completed_search_page(search="검사결과001 이상없음").total == 1


def test_late_completion_uses_actual_local_date_and_history_survives_rule_edit(history):
    _engine, tasks, _records = history
    task, start = _complete_series(tasks, 1)
    done = datetime(2026, 9, 30, 15, tzinfo=UTC)  # Oct 1 00:00 KST
    tasks.transition_occurrence(
        task.id, start, OccurrenceStatus.COMPLETED, now=done, result_note="늦게완료"
    )
    assert tasks.completed_on(date(2026, 9, 30)) == []
    found = tasks.completed_on(date(2026, 10, 1))
    assert len(found) == 1 and found[0].completed_at == done
    tasks.update(
        task.id, TaskDraft(title=task.title, starts_at=start, recurrence_rule="FREQ=WEEKLY")
    )
    assert tasks.completed_search_page(search="늦게완료").total == 1
    tasks.update_result_note(task.id, "과거요약수정", occurrence_start=start)
    assert tasks.completed_search_page(search="과거요약수정").total == 1
    tasks.move_to_trash(task.id, now=done)
    assert tasks.completed_search_page(search="과거요약수정").total == 0
    tasks.restore_from_trash(task.id, now=done)
    assert tasks.completed_search_page(search="과거요약수정").total == 1


def test_completion_history_pages_are_stable_bounded_and_mix_regular_tasks(history):
    engine, tasks, _records = history
    _task, start = _complete_series(tasks, 60)
    regular = tasks.create(TaskDraft(title="반복 일반", starts_at=start))
    tasks.complete(regular.id, now=start + timedelta(days=80), result_note="별도 결과")
    statements = []

    def count(_connection, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        pages = [
            tasks.completed_search_page(search="반복", offset=offset, limit=25)
            for offset in (0, 25, 50)
        ]
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert [len(page.items) for page in pages] == [25, 25, 11]
    assert all(page.total == 61 for page in pages)
    keys = [(task.id, task.occurrence_id) for page in pages for task in page.items]
    assert len(set(keys)) == 61
    assert pages[0].items[0].id == regular.id
    assert len(statements) <= 15  # Five queries per page, independent of row count.
    assert (
        tasks.completed_search_page(
            search="반복", date_from=date(2026, 9, 2), date_to=date(2026, 9, 2)
        ).total
        == 1
    )
    with pytest.raises(ValueError):
        tasks.completed_search_page(
            search="", date_from=date(2026, 10, 1), date_to=date(2026, 9, 1)
        )


def test_browser_separates_occurrence_results_logs_and_open_context(history, qtbot):
    _engine, tasks, records = history
    task, start = _complete_series(tasks, 2)
    for number in range(2):
        records.add_work_log(
            task_id=task.id,
            occurrence_start=start + timedelta(days=number),
            log_date=(start + timedelta(days=number)).date(),
            content=f"별도메모{number}",
        )
    dialog = WorkLogBrowserDialog(task_service=tasks, record_service=records)
    qtbot.addWidget(dialog)
    dialog.search_edit.setText("반복")
    dialog._search_timer.stop()
    dialog._refresh()
    assert dialog.list_widget.count() == 2
    contexts = set()
    for row in range(2):
        dialog.list_widget.setCurrentRow(row)
        context = dialog._selected_task_context
        contexts.add(context)
        number = (context[1] - start).days
        detail = dialog.detail.toPlainText()
        assert f"검사결과{number:03}" in detail
        assert f"별도메모{number}" in detail
        assert f"별도메모{1 - number}" not in detail
        assert "업무일지 1건" in detail
    assert contexts == {(task.id, start), (task.id, start + timedelta(days=1))}
    dialog._open_selected_records()
    assert dialog._record_dialogs[-1]._occurrence_start == dialog._selected_task_context[1]
    dialog.shutdown()


def test_browser_finds_late_completion_on_completion_day(history, qtbot):
    _engine, tasks, records = history
    task, start = _complete_series(tasks, 1)
    tasks.transition_occurrence(
        task.id,
        start,
        OccurrenceStatus.COMPLETED,
        now=start + timedelta(days=30),
        result_note="월말처리",
    )
    dialog = WorkLogBrowserDialog(task_service=tasks, record_service=records)
    qtbot.addWidget(dialog)
    dialog.date_edit.setDate(QDate(2026, 10, 1))
    assert dialog.list_widget.count() == 1
    dialog.list_widget.setCurrentRow(0)
    assert "월말처리" in dialog.detail.toPlainText()
    assert dialog._selected_task_context == (task.id, start)
    dialog.shutdown()


def test_history_search_includes_attached_filename_and_excludes_detached_file(history):
    engine, tasks, _records = history
    task, start = _complete_series(tasks, 2)
    sessions = SessionFactory(engine)
    with sessions.transaction() as session:
        attachment = AttachmentRecord(
            task_id=task.id,
            original_name="검사서 특이파일.pdf",
            stored_name="history-file.pdf",
            relative_path="history-file.pdf",
            size_bytes=1,
            checksum="0" * 64,
            created_at=start,
        )
        session.add(attachment)
        session.flush()
        attachment_id = attachment.id
    page = tasks.completed_search_page(search="특이파일")
    assert page.total == 2
    assert all(
        item.has_attachments and item.matched_attachment_id == attachment_id for item in page.items
    )
    with sessions.transaction() as session:
        session.get(AttachmentRecord, attachment_id).detached_at = start
    assert tasks.completed_search_page(search="특이파일").total == 0
