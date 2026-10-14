from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from officeflow.application.calendar_display import CalendarState, calendar_state
from officeflow.application.reminder_groups import group_reminders
from officeflow.application.reminder_preview import ReminderPreviewItem
from officeflow.application.tasks import ScheduledTask, TaskDraft, TaskService
from officeflow.domain.enums import OccurrenceStatus, TaskStatus
from officeflow.presentation.month_calendar import build_calendar_segments
from tests.unit.test_task_service import InMemoryTaskRepository


def scheduled(id, start, end=None, status=TaskStatus.ACTIVE):
    stamp = datetime(2026, 10, start, tzinfo=UTC)
    task = replace(
        TaskService(InMemoryTaskRepository()).create(
            TaskDraft(title=f"Task {id}", starts_at=stamp, status=status)
        ),
        id=id,
    )
    return ScheduledTask(task, stamp, datetime(2026, 10, end, tzinfo=UTC) if end else None)


@pytest.mark.parametrize(
    "occurrence,state",
    [
        (None, CalendarState.UNFINISHED),
        (OccurrenceStatus.PENDING, CalendarState.UNFINISHED),
        (OccurrenceStatus.COMPLETED, CalendarState.COMPLETED),
        (OccurrenceStatus.SKIPPED, CalendarState.INACTIVE),
        (OccurrenceStatus.CANCELED, CalendarState.INACTIVE),
    ],
)
def test_calendar_status_uses_occurrence_not_acknowledgement(occurrence, state):
    item = scheduled(1, 12)
    assert calendar_state(replace(item, occurrence_status=occurrence)) == state
    alarm = ReminderPreviewItem(
        item.task,
        1,
        item.starts_at,
        item.starts_at,
        item.starts_at,
        "확인함",
        occurrence_status=occurrence,
    )
    assert calendar_state(group_reminders((alarm,))[0]) == state


def test_whole_task_inactive_overrides_completed_occurrence():
    item = replace(
        scheduled(1, 12, status=TaskStatus.CANCELED), occurrence_status=OccurrenceStatus.COMPLETED
    )
    assert calendar_state(item) == CalendarState.INACTIVE


def test_completed_cannot_backfill_above_overlapping_unfinished():
    # Monday-Tuesday A, Tuesday-Wednesday B, completed Wednesday-Thursday C.
    a, b = scheduled(1, 12, 14), scheduled(2, 13, 15)
    c = scheduled(3, 14, 16, TaskStatus.COMPLETED)
    unrelated = scheduled(4, 16, status=TaskStatus.COMPLETED)
    bars = build_calendar_segments((c, unrelated, b, a), date(2026, 10, 11), "UTC")
    lanes = {bar.task.id: bar.lane for bar in bars}
    assert lanes == {1: 0, 2: 1, 3: 2, 4: 0}
    assert len(bars) == 4


@pytest.mark.parametrize("count", [60, 61, 120, 121])
def test_overview_counts_unfinished_outside_preview(task_service, count):
    stamp = datetime(2026, 10, 14, tzinfo=UTC)
    for index in range(count):
        task_service.create(
            TaskDraft(
                title=f"Calendar {index}",
                starts_at=stamp,
                ends_at=stamp + timedelta(hours=1),
                status=TaskStatus.ACTIVE if index == count - 1 else TaskStatus.COMPLETED,
            )
        )
    result = task_service.calendar_overview(date(2026, 10, 14), date(2026, 10, 15))
    assert result.day_unfinished_counts == ((date(2026, 10, 14), 1),)
    assert result.previews_complete == (count <= 60)
    if count <= 120:
        assert result.preview_tasks[0].status == TaskStatus.ACTIVE
        assert len(result.preview_tasks) == min(count, 60)
    else:
        assert result.summary_mode
        assert result.preview_tasks == ()


def test_calendar_search_recurrence_counts_and_read_only_queries(task_service):
    from sqlalchemy import event

    stamp = datetime(2026, 10, 14, tzinfo=UTC)
    recurring = task_service.create(
        TaskDraft(title="Find recurring", starts_at=stamp, recurrence_rule="FREQ=DAILY;COUNT=3")
    )
    task_service.create(
        TaskDraft(title="Find canceled", starts_at=stamp, status=TaskStatus.CANCELED)
    )
    task_service.create(TaskDraft(title="Other active", starts_at=stamp))
    task_service.transition_occurrence(recurring.id, stamp, OccurrenceStatus.COMPLETED)
    engine = task_service._repository._sessions._bind
    statements = []

    def trace(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", trace)
    try:
        result = task_service.calendar_overview(
            date(2026, 10, 14),
            date(2026, 10, 17),
            search="Find",
            preview_limit=1,
            summary_threshold=100,
        )
    finally:
        event.remove(engine, "before_cursor_execute", trace)
    assert result.day_unfinished_counts == (
        (date(2026, 10, 14), 0),
        (date(2026, 10, 15), 1),
        (date(2026, 10, 16), 1),
    )
    assert result.preview_tasks[0].occurrence_start == stamp + timedelta(days=1)
    assert not result.previews_complete
    assert len([s for s in statements if "UNION ALL" in s]) == 1
    assert not any(
        s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements
    )
