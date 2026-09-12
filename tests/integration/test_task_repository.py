from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from officeflow.application.tasks import TaskDraft, TaskService, TaskView
from officeflow.domain.enums import TaskPriority, TaskStatus
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository


def test_repository_round_trip_and_search(tmp_path: Path) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    service = TaskService(SqlAlchemyTaskRepository(SessionFactory(engine)))
    start = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)

    saved = service.create(
        TaskDraft(
            title="현장 점검",
            description="A동부터 C동까지 확인",
            priority=TaskPriority.IMPORTANT,
            all_day=True,
            starts_at=start,
            ends_at=start + timedelta(days=4),
        ),
        now=start,
    )

    assert saved.id is not None
    restored = service.get(saved.id)
    assert restored.starts_at == start
    assert restored.starts_at.tzinfo is UTC
    assert [task.id for task in service.list(TaskView.ALL, search="C동", now=start)] == [saved.id]

    updated = service.update(
        saved.id,
        TaskDraft(
            title="현장 점검 수정",
            description=restored.description,
            priority=TaskPriority.URGENT,
            all_day=True,
            starts_at=restored.starts_at,
            ends_at=restored.ends_at,
        ),
        now=start + timedelta(minutes=30),
    )
    assert updated.title == "현장 점검 수정"
    assert updated.priority is TaskPriority.URGENT

    completed = service.transition(saved.id, TaskStatus.COMPLETED, now=start + timedelta(hours=1))
    assert completed.completed_at == start + timedelta(hours=1)
    engine.dispose()
