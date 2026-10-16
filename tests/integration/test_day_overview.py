from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from officeflow.application.day_overview import DayOverviewService
from officeflow.application.tasks import TaskDraft
from officeflow.domain.occurrence import TaskOccurrence
from officeflow.infrastructure.database.models import TaskHistoryRecord, TaskOccurrenceRecord


def test_day_query_is_bounded_read_only_and_resumable(task_service):
    start = datetime(2026, 10, 15, tzinfo=UTC)
    task = task_service.create(
        TaskDraft(title="밀집", starts_at=start, recurrence_rule="FREQ=SECONDLY;COUNT=900")
    )
    repository = task_service._repository
    with repository._sessions.transaction() as session:
        before = session.scalar(select(func.count()).select_from(TaskHistoryRecord))
    service = DayOverviewService(task_service)
    first = service.query(date(2026, 10, 15), timezone="UTC", limit=100)
    assert len(first.items) == 100 and not first.complete and first.cursor is not None
    second = service.query(date(2026, 10, 15), timezone="UTC", limit=100, cursor=first.cursor)
    assert len(second.items) == 100
    assert not {i.occurrence_start for i in first.items} & {
        i.occurrence_start for i in second.items
    }
    assert all(item.id == task.id for item in first.items)
    with repository._sessions.transaction() as session:
        assert session.scalar(select(func.count()).select_from(TaskOccurrenceRecord)) == 0
        assert session.scalar(select(func.count()).select_from(TaskHistoryRecord)) == before


def test_moved_occurrence_keeps_original_identity_and_excludes_old_date(task_service):
    original = datetime(2026, 10, 1, 1, tzinfo=UTC)
    task = task_service.create(
        TaskDraft(title="이동 회차", starts_at=original, recurrence_rule="FREQ=DAILY;COUNT=2")
    )
    moved = datetime(2026, 10, 15, 5, tzinfo=UTC)
    task_service._repository.save_occurrence(
        TaskOccurrence(None, task.id, original, None, effective_start=moved)
    )
    service = DayOverviewService(task_service)
    old = service.query(date(2026, 10, 1), timezone="UTC")
    assert not old.items
    current = service.query(date(2026, 10, 15), timezone="UTC")
    assert len(current.items) == 1
    assert current.items[0].starts_at == moved
    assert current.items[0].occurrence_start == original


def test_day_search_and_midnight_are_independent_of_attachments(task_service):
    start = datetime(2026, 10, 15, 14, tzinfo=UTC)
    task_service.create(
        TaskDraft(title="야간 점검", starts_at=start, ends_at=start + timedelta(hours=3))
    )
    task_service.create(TaskDraft(title="다른 일정", starts_at=start))
    result = DayOverviewService(task_service).query(
        date(2026, 10, 16), timezone="Asia/Seoul", search="야간"
    )
    assert len(result.items) == 1 and result.complete
    assert result.items[0].starts_at == start


def test_day_query_cancellation_is_explicit_partial_result(task_service):
    result = DayOverviewService(task_service).query(
        date(2026, 10, 15), timezone="UTC", cancel_requested=lambda: True
    )
    assert not result.complete and result.cursor is not None


def test_five_thousand_tasks_use_bounded_day_source(task_service, record_property):
    from time import perf_counter

    from sqlalchemy import event

    from officeflow.domain.task import Task

    repository = task_service._repository
    start = datetime(2026, 10, 15, 9, tzinfo=UTC)
    with repository._sessions.transaction() as session:
        session.add_all(
            [
                repository._to_record(
                    Task.create(
                        title=f"합성 업무 {i}",
                        starts_at=start if i < 20 else start + timedelta(days=5),
                    )
                )
                for i in range(5000)
            ]
        )
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = repository._sessions._bind
    event.listen(engine, "before_cursor_execute", capture)
    try:
        began = perf_counter()
        result = DayOverviewService(task_service).query(start.date(), timezone="UTC")
        elapsed = perf_counter() - began
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert result.complete and len(result.items) == 20
    assert statements and all("LIMIT" in sql.upper() for sql in statements)
    record_property("day_5000_elapsed_ms", round(elapsed * 1000, 2))
    assert elapsed < 5
