from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from sqlalchemy import event, insert

from officeflow.application.task_history import HistoryQuery
from officeflow.application.tasks import TaskService
from officeflow.domain.task_history import encode
from officeflow.infrastructure.database.models import TaskHistoryRecord, TaskSnapshotRecord
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository
from officeflow.tools.sample_data import generate


def test_fifty_thousand_history_and_snapshot_metadata_pages_are_bounded(tmp_path):
    path = tmp_path / "large.db"
    generate(path, 5_000)
    engine = create_database_engine(path)
    sessions = SessionFactory(engine)
    now = datetime.now(UTC)
    info = encode({"values": {"description": "원문" * 250}, "checklist": []})
    with engine.begin() as connection:
        for offset in range(0, 50_000, 1_000):
            connection.execute(
                insert(TaskHistoryRecord),
                [
                    dict(
                        task_id=1,
                        scope="task",
                        scope_key="task",
                        occurrence_start=None,
                        command_id=str(index),
                        event_type="change",
                        recorded_at=now,
                        changed_fields='["description"]',
                        before_payload=info,
                        after_payload=info,
                        payload_version=1,
                        summary="설명 변경",
                    )
                    for index in range(offset, offset + 1_000)
                ],
            )
            connection.execute(
                insert(TaskSnapshotRecord),
                [
                    dict(
                        task_id=1,
                        scope="task",
                        scope_key="task",
                        occurrence_start=None,
                        label="중요 시점",
                        ordinal=index + 1,
                        captured_at=now,
                        committed_at=now,
                        job_id=uuid4().hex,
                        request_fingerprint="a" * 64,
                        payload_version=1,
                        info_payload=info,
                        file_count=0,
                        summary="중요 시점",
                    )
                    for index in range(offset, offset + 1_000)
                ],
            )
    queries = []

    def inspect(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", inspect)
    try:
        history = TaskService(SqlAlchemyTaskRepository(sessions)).history
        started = perf_counter()
        first = history.page(1)
        first_ms = (perf_counter() - started) * 1000
        last = first.items[-1]
        started = perf_counter()
        next_page = history.page(
            1, HistoryQuery(cursor=(last.recorded_at, 1 if last.kind == "snapshot" else 0, last.id))
        )
        next_ms = (perf_counter() - started) * 1000
        assert len(first.items) == len(next_page.items) == 50
        assert first.has_next and next_page.has_next
        assert not {(x.kind, x.id) for x in first.items} & {(x.kind, x.id) for x in next_page.items}
        assert all(
            "before_payload" not in sql and "after_payload" not in sql and "info_payload" not in sql
            for sql in queries
        )
        assert first_ms < 200 and next_ms < 100
        print(
            f"HISTORY_PERFORMANCE tasks=5000 history=50000 snapshots=50000 first_ms={first_ms:.2f} next_ms={next_ms:.2f}"
        )
    finally:
        engine.dispose()
