from dataclasses import replace
from datetime import UTC, datetime, timedelta

from alembic import command
from sqlalchemy import select, update

from officeflow.application.completed_browse import CompletionRequest, month_bounds
from officeflow.application.tasks import TaskDraft, TaskQuery, TaskView
from officeflow.domain.enums import OccurrenceStatus, TaskPriority, TaskStatus
from officeflow.infrastructure.database.completed_browse_repository import (
    SqlAlchemyCompletedBrowseRepository,
)
from officeflow.infrastructure.database.migrate import migration_config, upgrade_database
from officeflow.infrastructure.database.models import TaskRecord

NOW = datetime(2026, 10, 26, 3, tzinfo=UTC)


def repository(service):
    return SqlAlchemyCompletedBrowseRepository(service._repository._sessions)


def request(**kwargs):
    return CompletionRequest(TaskQuery(view=TaskView.COMPLETED), "Asia/Seoul", **kwargs)


def test_completed_scopes_unknown_reopen_delete_and_flat_match(task_service):
    service = task_service
    plain = service.create(TaskDraft(title="plain", status=TaskStatus.COMPLETED), now=NOW)
    unknown = service.create(TaskDraft(title="unknown", status=TaskStatus.COMPLETED), now=NOW)
    recurring = service.create(
        TaskDraft(title="recurring", starts_at=NOW, recurrence_rule="FREQ=DAILY"), now=NOW
    )
    service.transition_occurrence(
        recurring.id, NOW, OccurrenceStatus.COMPLETED, now=NOW + timedelta(days=1)
    )
    service.complete(recurring.id, now=NOW + timedelta(days=2))
    with service._repository._sessions.transaction() as session:
        session.execute(
            update(TaskRecord).where(TaskRecord.id == unknown.id).values(completed_at=None)
        )
    repo = repository(service)
    monthly = repo.read(request(expanded=((2026, 10), None)))
    flat = repo.read(request(flat=True))
    assert monthly.total == flat.total == 4
    assert {(b.month, b.count) for b in monthly.buckets} == {((2026, 10), 3), (None, 1)}
    entries = [entry for page in monthly.pages for entry in page.entries]
    assert {entry.key.scope for entry in entries if entry.key.task_id == recurring.id} == {
        "task",
        "occurrence",
    }
    assert (
        next(e for e in entries if e.key.scope == "occurrence").key.original_occurrence_start == NOW
    )
    service.transition(plain.id, TaskStatus.ACTIVE)
    service.move_to_trash(unknown.id)
    assert repo.read(request()).total == 2


def test_pages_are_keyset_stable_bounded_and_filters_match(task_service):
    service = task_service
    for i in range(101):
        service.create(
            TaskDraft(
                title=f"done {i}",
                status=TaskStatus.COMPLETED,
                is_pinned=i % 2 == 0,
                priority=TaskPriority.IMPORTANT if i % 2 == 0 else TaskPriority.NORMAL,
            ),
            now=NOW,
        )
    repo = repository(service)
    first = repo.read(request())
    assert first.total == 101
    page = first.pages[0]
    assert len(page.entries) == 50 and page.next_cursor is not None
    second = repo.read(request(expanded=((2026, 10),), cursors=(((2026, 10), page.next_cursor),)))
    third = repo.read(
        request(expanded=((2026, 10),), cursors=(((2026, 10), second.pages[0].next_cursor),))
    )
    keys = [e.key for result in (first, second, third) for e in result.pages[0].entries]
    assert len(keys) == len(set(keys)) == 101
    for query, count in (
        (TaskQuery(pinned_only=True), 51),
        (TaskQuery(priorities=frozenset({TaskPriority.IMPORTANT})), 51),
        (TaskQuery(has_attachments=True), 0),
        (TaskQuery(statuses=frozenset({TaskStatus.ACTIVE})), 0),
        (TaskQuery(search="doesnotexist"), 0),
    ):
        result = repo.read(replace(request(), query=query))
        flat = repo.read(replace(request(flat=True), query=query))
        assert result.total == flat.total == count
        assert sum(bucket.count for bucket in result.buckets) == count


def test_old_period_skips_empty_years_and_month_timezone(task_service):
    service = task_service
    service.create(TaskDraft(title="now", status=TaskStatus.COMPLETED), now=NOW)
    service.create(TaskDraft(title="old", status=TaskStatus.COMPLETED), now=NOW.replace(year=2010))
    repo = repository(service)
    first = repo.read(request())
    assert first.has_older
    second = repo.read(request(before=first.oldest_start))
    assert second.buckets[0].month == (2010, 10)
    assert not second.has_older
    start, end = month_bounds((2024, 3), "America/New_York")
    assert start.hour == 5 and end.hour == 4
    assert (end - start).total_seconds() == 31 * 86400 - 3600
    start, end = month_bounds((2024, 2), "Asia/Seoul")
    assert end - start == timedelta(days=29)


def test_occurrence_search_does_not_match_sibling_result(task_service):
    task = task_service.create(
        TaskDraft(title="template", starts_at=NOW, recurrence_rule="FREQ=DAILY"), now=NOW
    )
    for i, result in enumerate(("uniquetext", "other")):
        task_service.transition_occurrence(
            task.id,
            NOW + timedelta(days=i),
            OccurrenceStatus.COMPLETED,
            result_note=result,
            now=NOW,
        )
    result = repository(task_service).read(replace(request(), query=TaskQuery(search="uniquetext")))
    assert result.total == 1
    assert result.pages[0].entries[0].key.original_occurrence_start == NOW


def test_index_only_upgrade_preserves_rows_and_protects_old_db(task_service, tmp_path):
    service = task_service
    service.create(TaskDraft(title="preserve", status=TaskStatus.COMPLETED), now=NOW)
    sessions = service._repository._sessions
    database = sessions._bind.url.database
    from pathlib import Path

    database = Path(database)
    command.downgrade(migration_config(database), "0016_task_folder_links")
    with sessions.transaction() as session:
        before = session.execute(select(TaskRecord.__table__)).all()
    upgrade_database(database)
    with sessions.transaction() as session:
        assert session.execute(select(TaskRecord.__table__)).all() == before
    assert list((database.parent / "schema-backups").glob("*.db"))


def test_large_completion_queries_are_bounded_and_use_index(task_service):
    from time import perf_counter

    from sqlalchemy import insert, text

    from officeflow.domain.task import Task
    from officeflow.infrastructure.database.models import TaskOccurrenceRecord

    service = task_service
    sessions = service._repository._sessions
    # Small metadata only; no attachment bytes or production profile.
    with sessions.transaction() as session:
        for start in range(0, 5000, 500):
            records = [
                service._repository._to_record(
                    Task.create(title=f"synthetic {i}", status=TaskStatus.COMPLETED, now=NOW)
                )
                for i in range(start, start + 500)
            ]
            session.add_all(records)
            session.flush()
    recurring = service.create(
        TaskDraft(
            title="synthetic recurrence",
            starts_at=NOW.replace(year=1700),
            recurrence_rule="FREQ=DAILY",
        ),
        now=NOW,
    )
    origin = NOW.replace(year=1700)
    with sessions.transaction() as session:
        for start in range(0, 100000, 1000):
            session.execute(
                insert(TaskOccurrenceRecord),
                [
                    {
                        "task_id": recurring.id,
                        "occurrence_start": origin + timedelta(days=i),
                        "status": "completed",
                        "completed_at": origin + timedelta(days=i),
                        "result_note": "",
                    }
                    for i in range(start, start + 1000)
                ],
            )
    repo = repository(service)
    start = perf_counter()
    result = repo.read(request())
    elapsed = perf_counter() - start
    assert result.total == 105000
    assert len(result.buckets) <= 7
    assert sum(len(page.entries) for page in result.pages) <= 100
    with sessions.transaction() as session:
        plan = session.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT id FROM task_occurrences WHERE status='completed' AND completed_at>='2026-01-01' ORDER BY completed_at DESC,task_id DESC,id DESC LIMIT 51"
            )
        ).all()
    assert any("ix_task_occurrences_completed" in str(row) for row in plan)
    print(
        f"completion benchmark: tasks=5001 completions=105000 returned={sum(len(p.entries) for p in result.pages)} seconds={elapsed:.4f} plan={plan}",
        flush=True,
    )
    assert elapsed < 2.0
