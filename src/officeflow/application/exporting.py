from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from officeflow.application.tasks import TaskQuery, TaskService
from officeflow.domain.enums import TaskStatus
from officeflow.domain.task import Task


class ExportCanceledError(RuntimeError):
    """Raised when the user cancels an export before it is committed."""


class CalendarExportScope(StrEnum):
    CURRENT_LIST = "current_list"
    SELECTED_TASKS = "selected_tasks"
    DATE_RANGE = "date_range"


@dataclass(frozen=True, slots=True)
class CalendarExportOptions:
    scope: CalendarExportScope
    query: TaskQuery | None = None
    selected_task_ids: tuple[int, ...] = ()
    date_from: date | None = None
    date_to: date | None = None
    include_recurring: bool = False
    include_completed: bool = True

    def __post_init__(self) -> None:
        if self.scope is CalendarExportScope.CURRENT_LIST and self.query is None:
            raise ValueError("현재 목록 내보내기에는 현재 조회 조건이 필요합니다.")
        if self.scope is CalendarExportScope.SELECTED_TASKS and not self.selected_task_ids:
            raise ValueError("내보낼 업무를 먼저 선택하세요.")
        if self.scope is CalendarExportScope.DATE_RANGE:
            if self.date_from is None or self.date_to is None:
                raise ValueError("내보낼 시작일과 종료일을 지정하세요.")
            if self.date_from > self.date_to:
                raise ValueError("내보내기 시작일은 종료일보다 늦을 수 없습니다.")


@dataclass(frozen=True, slots=True)
class CalendarExportResult:
    path: Path
    exported_count: int
    excluded_recurring_count: int
    excluded_completed_count: int
    excluded_unscheduled_count: int


class TaskExporter(Protocol):
    def export(
        self,
        tasks: tuple[Task, ...],
        destination: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path: ...


class ExportService:
    def __init__(
        self,
        task_service: TaskService,
        excel_exporter: TaskExporter,
        calendar_exporter: TaskExporter,
    ) -> None:
        self._task_service = task_service
        self._excel_exporter = excel_exporter
        self._calendar_exporter = calendar_exporter

    def export_excel(
        self,
        query: TaskQuery,
        destination: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        tasks = self._all_matching(query)
        return self._excel_exporter.export(
            tasks, destination, cancel_requested=cancel_requested
        )

    def export_calendar(
        self,
        destination: Path,
        *,
        options: CalendarExportOptions | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> CalendarExportResult:
        effective_options = options or CalendarExportOptions(
            scope=CalendarExportScope.CURRENT_LIST,
            query=TaskQuery(),
            include_recurring=True,
        )
        tasks = self._calendar_source(effective_options)
        active = tuple(task for task in tasks if task.deleted_at is None)
        excluded_unscheduled = sum(task.starts_at is None for task in active)
        scheduled = tuple(task for task in active if task.starts_at is not None)
        excluded_recurring = sum(
            task.recurrence_rule is not None for task in scheduled
        )
        if effective_options.include_recurring:
            recurrence_filtered = scheduled
        else:
            recurrence_filtered = tuple(
                task for task in scheduled if task.recurrence_rule is None
            )
        excluded_completed = sum(
            task.status is TaskStatus.COMPLETED for task in recurrence_filtered
        )
        if effective_options.include_completed:
            status_filtered = recurrence_filtered
        else:
            status_filtered = tuple(
                task
                for task in recurrence_filtered
                if task.status is not TaskStatus.COMPLETED
            )
        normalized = self._original_recurring_tasks(status_filtered)
        if not normalized:
            raise ValueError("선택한 조건에 내보낼 일정이 없습니다.")
        path = self._calendar_exporter.export(
            normalized, destination, cancel_requested=cancel_requested
        )
        return CalendarExportResult(
            path=path,
            exported_count=len(normalized),
            excluded_recurring_count=(
                0 if effective_options.include_recurring else excluded_recurring
            ),
            excluded_completed_count=(
                0 if effective_options.include_completed else excluded_completed
            ),
            excluded_unscheduled_count=excluded_unscheduled,
        )

    def _calendar_source(self, options: CalendarExportOptions) -> tuple[Task, ...]:
        if options.scope is CalendarExportScope.CURRENT_LIST:
            assert options.query is not None
            return self._all_matching(options.query)
        if options.scope is CalendarExportScope.SELECTED_TASKS:
            tasks_by_id = self._task_service.get_many(options.selected_task_ids)
            return tuple(
                tasks_by_id[task_id]
                for task_id in options.selected_task_ids
                if task_id in tasks_by_id
            )
        assert options.date_from is not None and options.date_to is not None
        return self._task_service.calendar_range(
            options.date_from,
            options.date_to + timedelta(days=1),
        )

    def _original_recurring_tasks(self, tasks: tuple[Task, ...]) -> tuple[Task, ...]:
        recurring_ids = tuple(
            dict.fromkeys(
                task.id
                for task in tasks
                if task.id is not None and task.recurrence_rule is not None
            )
        )
        if not recurring_ids:
            return tasks
        originals = self._task_service.get_many(recurring_ids)
        normalized: list[Task] = []
        seen: set[int] = set()
        for task in tasks:
            if task.id is not None:
                if task.id in seen:
                    continue
                seen.add(task.id)
            normalized.append(originals.get(task.id, task) if task.id is not None else task)
        return tuple(normalized)

    def _all_matching(self, query: TaskQuery) -> tuple[Task, ...]:
        page = self._task_service.query(replace(query, offset=0, limit=None))
        return page.items
