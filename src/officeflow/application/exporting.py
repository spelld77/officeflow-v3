from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from officeflow.application.tasks import TaskQuery, TaskService
from officeflow.domain.task import Task


class ExportCanceledError(RuntimeError):
    """Raised when the user cancels an export before it is committed."""


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
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        tasks = self._all_matching(TaskQuery())
        scheduled = tuple(task for task in tasks if task.starts_at is not None)
        return self._calendar_exporter.export(
            scheduled, destination, cancel_requested=cancel_requested
        )

    def _all_matching(self, query: TaskQuery) -> tuple[Task, ...]:
        page = self._task_service.query(replace(query, offset=0, limit=None))
        return page.items
