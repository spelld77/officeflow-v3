from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from officeflow.application.attachments import AttachmentService
from officeflow.application.tasks import TaskDraft, TaskQuery, TaskService, TaskView
from officeflow.infrastructure.attachments.storage import ManagedAttachmentStorage
from officeflow.infrastructure.database.attachment_repository import (
    SqlAlchemyAttachmentRepository,
)
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository


def test_attachment_repository_round_trip_and_task_filter(tmp_path: Path) -> None:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    sessions = SessionFactory(engine)
    task_service = TaskService(SqlAlchemyTaskRepository(sessions))
    repository = SqlAlchemyAttachmentRepository(sessions)
    service = AttachmentService(
        repository,
        ManagedAttachmentStorage(tmp_path / "attachments"),
        task_service,
    )
    attached_task = task_service.create(TaskDraft(title="첨부 업무"))
    plain_task = task_service.create(TaskDraft(title="일반 업무"))
    assert attached_task.id is not None and plain_task.id is not None
    source = tmp_path / "계약서.txt"
    source.write_text("contract", encoding="utf-8")

    attachment = service.attach(
        attached_task.id,
        source,
        now=datetime(2026, 9, 15, tzinfo=UTC),
    )
    page = task_service.query(
        TaskQuery(view=TaskView.ALL, has_attachments=True, limit=None)
    )

    assert attachment.original_name == "계약서.txt"
    assert attachment.checksum is not None
    assert [task.id for task in page.items] == [attached_task.id]
    assert page.items[0].has_attachments
    assert not task_service.get(plain_task.id).has_attachments
    engine.dispose()
