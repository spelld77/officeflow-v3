from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from officeflow.application.tasks import TaskService
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository


@pytest.fixture
def task_service(tmp_path: Path) -> Iterator[TaskService]:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    service = TaskService(SqlAlchemyTaskRepository(SessionFactory(engine)))
    yield service
    engine.dispose()
