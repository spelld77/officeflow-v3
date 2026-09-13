from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

from officeflow.domain.task import Task
from officeflow.presentation.month_calendar import (
    build_calendar_segments,
    month_grid_start,
    task_date_span,
    tasks_for_date,
)


def _task(title: str, start: datetime, end: datetime | None, task_id: int = 1) -> Task:
    return replace(
        Task.create(title=title, all_day=True, starts_at=start, ends_at=end),
        id=task_id,
    )


def test_month_grid_is_monday_first_and_always_covers_six_weeks() -> None:
    start = month_grid_start(2026, 9)

    assert start == date(2026, 8, 31)
    assert start + (date(2026, 10, 11) - start) == date(2026, 10, 11)


def test_exclusive_end_is_rendered_as_inclusive_final_date() -> None:
    task = _task(
        "출장",
        datetime(2026, 9, 3, 15, 0, tzinfo=UTC),
        datetime(2026, 9, 10, 15, 0, tzinfo=UTC),
    )

    assert task_date_span(task, "Asia/Seoul") == (date(2026, 9, 4), date(2026, 9, 10))
    assert tasks_for_date((task,), date(2026, 9, 10), "Asia/Seoul") == (task,)
    assert tasks_for_date((task,), date(2026, 9, 11), "Asia/Seoul") == ()


def test_multiday_bar_splits_at_week_boundary_with_continuation_markers() -> None:
    task = _task(
        "주간 출장",
        datetime(2026, 9, 3, 15, 0, tzinfo=UTC),
        datetime(2026, 9, 10, 15, 0, tzinfo=UTC),
    )

    segments = build_calendar_segments((task,), month_grid_start(2026, 9), "Asia/Seoul")

    assert len(segments) == 2
    assert (segments[0].week, segments[0].start_column, segments[0].end_column) == (0, 4, 6)
    assert segments[0].continues_before is False
    assert segments[0].continues_after is True
    assert (segments[1].week, segments[1].start_column, segments[1].end_column) == (1, 0, 3)
    assert segments[1].continues_before is True
    assert segments[1].continues_after is False


def test_month_boundary_schedule_remains_visible_in_adjacent_cells() -> None:
    task = _task(
        "월말 점검",
        datetime(2026, 9, 28, 15, 0, tzinfo=UTC),
        datetime(2026, 10, 2, 15, 0, tzinfo=UTC),
    )

    segments = build_calendar_segments((task,), month_grid_start(2026, 9), "Asia/Seoul")

    assert len(segments) == 1
    assert (segments[0].start_column, segments[0].end_column) == (1, 4)
