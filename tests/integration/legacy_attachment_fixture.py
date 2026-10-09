"""Create genuine historical schemas; never downgrade a file-preserving schema."""

import hashlib
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from alembic import command
from sqlalchemy import text

from officeflow.application.tasks import TaskService
from officeflow.infrastructure.database.migrate import migration_config
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository


@contextmanager
def legacy_environment(root, revision="0012_attachment_observation", *, attachment_root=None):
    root.mkdir(parents=True, exist_ok=True)
    database = root / "officeflow.db"
    command.upgrade(migration_config(database), revision)
    engine = create_database_engine(database)
    sessions = SessionFactory(engine)
    tasks = TaskService(SqlAlchemyTaskRepository(sessions))
    folder = attachment_root or root / "attachments"
    folder.mkdir(parents=True, exist_ok=True)

    def add(task_id, name, *, created=None, content=b"legacy saved content", detached_at=None):
        timestamp = created or datetime(2026, 10, 1, 3, tzinfo=UTC)
        token = uuid4().hex
        relative = f"{token[:2]}/{token[2:4]}/{token}.txt"
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        with engine.begin() as connection:
            result = connection.execute(
                text(
                    "INSERT INTO attachments(task_id,original_name,stored_name,relative_path,size_bytes,checksum,created_at,detached_at) VALUES (:task,:name,:stored,:relative,:size,:checksum,:created,:detached)"
                ),
                {
                    "task": task_id,
                    "name": name,
                    "stored": path.name,
                    "relative": relative,
                    "size": len(content),
                    "checksum": digest,
                    "created": timestamp.replace(tzinfo=None).isoformat(sep=" "),
                    "detached": detached_at.replace(tzinfo=None).isoformat(sep=" ")
                    if detached_at
                    else None,
                },
            )
            attachment_id = result.lastrowid
        return SimpleNamespace(
            id=attachment_id,
            id_required=attachment_id,
            task_id=task_id,
            original_name=name,
            relative_path=relative,
            checksum=digest,
            path=path,
        )

    try:
        yield SimpleNamespace(
            engine=engine,
            database=database,
            sessions=sessions,
            tasks=tasks,
            add=add,
            attachment_root=folder,
        )
    finally:
        engine.dispose()
