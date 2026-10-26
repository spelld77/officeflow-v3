from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from officeflow.application.tasks import TaskQuery

Month = tuple[int, int] | None


def month_shift(year: int, month: int, delta: int) -> tuple[int, int]:
    absolute = year * 12 + month - 1 + delta
    return absolute // 12, absolute % 12 + 1


def month_bounds(month: tuple[int, int], timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    year, number = month
    next_year, next_month = month_shift(year, number, 1)
    return (
        datetime(year, number, 1, tzinfo=zone).astimezone(UTC),
        datetime(next_year, next_month, 1, tzinfo=zone).astimezone(UTC),
    )


@dataclass(frozen=True, slots=True)
class CompletedEntryKey:
    task_id: int
    original_occurrence_start: datetime | None
    scope: str


@dataclass(frozen=True, slots=True)
class CompletionCursor:
    completed_at: datetime | None
    task_id: int
    scope_rank: int
    occurrence_id: int


@dataclass(frozen=True, slots=True)
class CompletedEntry:
    key: CompletedEntryKey
    title: str
    completed_at: datetime | None
    recurrence: bool
    cursor: CompletionCursor


@dataclass(frozen=True, slots=True)
class CompletionBucket:
    month: Month
    count: int


@dataclass(frozen=True, slots=True)
class CompletionPage:
    month: Month
    entries: tuple[CompletedEntry, ...]
    next_cursor: CompletionCursor | None


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    query: TaskQuery
    timezone: str
    before: datetime | None = None
    expanded: tuple[Month, ...] | None = None
    cursors: tuple[tuple[Month, CompletionCursor | None], ...] = ()
    flat: bool = False


@dataclass(frozen=True, slots=True)
class CompletionResult:
    total: int
    buckets: tuple[CompletionBucket, ...]
    pages: tuple[CompletionPage, ...]
    oldest_start: datetime | None
    has_older: bool
    period_count: int


class CompletedBrowseRepository(Protocol):
    def read(self, request: CompletionRequest) -> CompletionResult: ...
