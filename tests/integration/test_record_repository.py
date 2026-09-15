from __future__ import annotations

from datetime import date
from pathlib import Path

from officeflow.application.records import RecordService
from officeflow.application.tasks import TaskDraft, TaskQuery, TaskService, TaskView
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.record_repository import SqlAlchemyRecordRepository
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository


def test_record_repository_round_trip_and_task_search(tmp_path: Path) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    sessions = SessionFactory(engine)
    task_service = TaskService(SqlAlchemyTaskRepository(sessions))
    record_service = RecordService(SqlAlchemyRecordRepository(sessions), task_service)
    task = task_service.create(TaskDraft(title="주간 보고"))
    assert task.id is not None

    first = record_service.add_checklist_item(task.id, "자료 취합")
    second = record_service.add_checklist_item(task.id, "보고서 작성")
    assert first.id is not None and second.id is not None
    record_service.set_checklist_done(first, True)
    record_service.reorder_checklist(task.id, (second.id, first.id))
    work_log = record_service.add_work_log(
        task_id=task.id,
        log_date=date(2026, 9, 15),
        content="매출지표 검증",
        result="오류 없음",
    )

    assert [item.id for item in record_service.checklist_for_task(task.id)] == [
        second.id,
        first.id,
    ]
    assert record_service.work_logs(log_date=date(2026, 9, 15)) == (work_log,)
    search = task_service.query(TaskQuery(view=TaskView.ALL, search="매출지표"))
    assert [item.id for item in search.items] == [task.id]

    record_service.delete_checklist_item(first.id)
    assert [item.position for item in record_service.checklist_for_task(task.id)] == [0]
    assert work_log.id is not None
    record_service.delete_work_log(work_log.id)
    assert record_service.work_logs(task_id=task.id) == ()
    engine.dispose()
