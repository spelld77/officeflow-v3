"""Bounded, read-only day projection. No occurrence cache/delivery is created."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from time import monotonic
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from officeflow.application.tasks import ScheduledTask, TaskService
from officeflow.domain.enums import OccurrenceStatus
from officeflow.domain.occurrence import TaskOccurrence
from officeflow.domain.recurrence import next_recurrence_start
from officeflow.domain.task import Task


@dataclass(frozen=True, slots=True)
class DayCursor:
    task_id: int = 0
    phase: str = "done"
    generated_after: datetime | None = None
    moved_after: int = 0


@dataclass(frozen=True, slots=True)
class DayOverviewResult:
    day: date
    items: tuple[ScheduledTask, ...]
    complete: bool
    cursor: DayCursor | None = None
    search: str = ""
    requested_cursor: DayCursor | None = None


class DayRepository(Protocol):
    def read_snapshot(self) -> AbstractContextManager[DayRepository]: ...
    def day_templates_page(
        self,
        starts_at: datetime,
        ends_at: datetime,
        *,
        after_id: int,
        include_after: bool,
        search: str,
        limit: int,
    ) -> tuple[Task, ...]: ...
    def get_occurrence(self, task_id: int, occurrence_start: datetime) -> TaskOccurrence | None: ...
    def day_moved_page(
        self,
        task_id: int,
        starts_at: datetime,
        ends_at: datetime,
        *,
        original_from: datetime,
        after_id: int,
        limit: int,
    ) -> tuple[TaskOccurrence, ...]: ...


def overlaps(start: datetime, end: datetime | None, lower: datetime, upper: datetime) -> bool:
    return start < upper and (end > lower if end is not None else start >= lower)


class DayOverviewService:
    def __init__(self, tasks: TaskService) -> None:
        self._repository = cast(DayRepository, tasks._repository)

    def query(
        self,
        day: date,
        *,
        timezone: str,
        search: str = "",
        limit: int = 500,
        seconds: float = 2.0,
        cursor: DayCursor | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> DayOverviewResult:
        if not 1 <= limit <= 500 or seconds <= 0:
            raise ValueError("하루 조회는 1~500개와 양수 시간 예산이 필요합니다.")
        zone = ZoneInfo(timezone)
        lower = datetime.combine(day, time.min, zone).astimezone(UTC)
        upper = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
        began = monotonic()
        position = cursor or DayCursor()
        items: list[ScheduledTask] = []

        def exhausted() -> bool:
            return monotonic() - began >= seconds or bool(cancel_requested and cancel_requested())

        def result(complete: bool) -> DayOverviewResult:
            return DayOverviewResult(
                day,
                tuple(
                    sorted(
                        items,
                        key=lambda i: (i.starts_at, i.id or 0, i.occurrence_start or i.starts_at),
                    )
                ),
                complete,
                None if complete else position,
                search,
                cursor,
            )

        with self._repository.read_snapshot() as reader:
            while not exhausted():
                templates = reader.day_templates_page(
                    lower,
                    upper,
                    after_id=position.task_id,
                    include_after=position.phase != "done",
                    search=search,
                    limit=200,
                )
                if not templates:
                    return result(True)
                for task in templates:
                    if exhausted():
                        return result(False)
                    assert task.id is not None and task.starts_at is not None
                    continuing = task.id == position.task_id and position.phase != "done"
                    if not task.recurrence_rule:
                        items.append(ScheduledTask(task, task.starts_at, task.ends_at))
                        position = DayCursor(task.id)
                    else:
                        duration = task.ends_at - task.starts_at if task.ends_at else None
                        try:
                            search_lower = lower - (duration or timedelta(0))
                        except OverflowError:
                            search_lower = datetime.min.replace(tzinfo=UTC)
                        if not continuing or position.phase != "moved":
                            after = position.generated_after if continuing else None
                            after = after or search_lower
                            inclusive = not continuing or position.generated_after is None
                            # Bound examined candidates, not merely visible output.
                            examined = 0
                            while True:
                                if exhausted() or examined >= 500:
                                    return result(False)
                                occurrence_start = next_recurrence_start(
                                    task.recurrence_rule,
                                    template_start=task.starts_at,
                                    timezone=task.timezone,
                                    after=after,
                                    inclusive=inclusive,
                                )
                                if occurrence_start is None or occurrence_start >= upper:
                                    break
                                examined += 1
                                saved = reader.get_occurrence(task.id, occurrence_start)
                                occurrence_end = (
                                    occurrence_start + duration if duration is not None else None
                                )
                                start = saved.starts_at if saved else occurrence_start
                                end = saved.ends_at if saved else occurrence_end
                                position = DayCursor(task.id, "generated", occurrence_start)
                                after, inclusive = occurrence_start, False
                                if saved and saved.status in {
                                    OccurrenceStatus.SKIPPED,
                                    OccurrenceStatus.CANCELED,
                                }:
                                    continue
                                if overlaps(start, end, lower, upper):
                                    items.append(
                                        ScheduledTask(
                                            task,
                                            start,
                                            end,
                                            occurrence_start,
                                            saved.status if saved else OccurrenceStatus.PENDING,
                                            saved.completed_at if saved else None,
                                        )
                                    )
                                if len(items) >= limit:
                                    return result(False)
                            position = DayCursor(task.id, "moved")
                        while True:
                            if exhausted():
                                return result(False)
                            moved = reader.day_moved_page(
                                task.id,
                                lower,
                                upper,
                                original_from=search_lower,
                                after_id=position.moved_after,
                                limit=200,
                            )
                            for occurrence in moved:
                                assert occurrence.id is not None
                                position = DayCursor(task.id, "moved", moved_after=occurrence.id)
                                items.append(
                                    ScheduledTask(
                                        task,
                                        occurrence.starts_at,
                                        occurrence.ends_at,
                                        occurrence.occurrence_start,
                                        occurrence.status,
                                        occurrence.completed_at,
                                    )
                                )
                                if len(items) >= limit:
                                    return result(False)
                            if len(moved) < 200:
                                break
                        position = DayCursor(task.id)
                    if len(items) >= limit:
                        return result(False)
                if len(templates) < 200:
                    return result(True)
        return result(False)
