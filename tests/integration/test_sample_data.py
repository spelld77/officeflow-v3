from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from officeflow.infrastructure.database.models import TaskRecord
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.tools.sample_data import generate


def test_sample_data_generator_creates_requested_number_of_tasks(tmp_path: Path) -> None:
    database_file = tmp_path / "sample.db"

    generate(database_file, 50)

    engine = create_database_engine(database_file)
    sessions = SessionFactory(engine)
    with sessions.transaction() as session:
        count = session.scalar(select(func.count()).select_from(TaskRecord))
    engine.dispose()

    assert count == 50
