from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from officeflow.application.tasks import TaskService
from officeflow.domain.enums import TaskPriority
from officeflow.domain.records import ChecklistItem, WorkLog


class RecordRepository(Protocol):
    def list_checklist(self, task_id: int) -> tuple[ChecklistItem, ...]: ...

    def add_checklist_item(self, item: ChecklistItem) -> ChecklistItem: ...

    def update_checklist_item(self, item: ChecklistItem) -> ChecklistItem: ...

    def delete_checklist_item(self, item_id: int) -> None: ...

    def reorder_checklist(self, task_id: int, item_ids: tuple[int, ...]) -> None: ...

    def get_work_log(self, log_id: int) -> WorkLog | None: ...

    def list_work_logs(
        self,
        *,
        log_date: date | None = None,
        task_id: int | None = None,
    ) -> tuple[WorkLog, ...]: ...

    def add_work_log(self, work_log: WorkLog) -> WorkLog: ...

    def update_work_log(self, work_log: WorkLog) -> WorkLog: ...

    def delete_work_log(self, log_id: int) -> None: ...


class RecordService:
    def __init__(self, repository: RecordRepository, task_service: TaskService) -> None:
        self._repository = repository
        self._task_service = task_service

    def checklist_for_task(self, task_id: int) -> tuple[ChecklistItem, ...]:
        self._task_service.get(task_id)
        return self._repository.list_checklist(task_id)

    def add_checklist_item(self, task_id: int, content: str) -> ChecklistItem:
        self._task_service.get(task_id)
        position = len(self._repository.list_checklist(task_id))
        return self._repository.add_checklist_item(
            ChecklistItem(
                id=None,
                task_id=task_id,
                content=content,
                is_done=False,
                position=position,
            )
        )

    def edit_checklist_item(self, item: ChecklistItem, content: str) -> ChecklistItem:
        return self._repository.update_checklist_item(item.edit(content))

    def set_checklist_done(
        self,
        item: ChecklistItem,
        is_done: bool,
        *,
        now: datetime | None = None,
    ) -> ChecklistItem:
        return self._repository.update_checklist_item(item.set_done(is_done, now=now))

    def delete_checklist_item(self, item_id: int) -> None:
        self._repository.delete_checklist_item(item_id)

    def reorder_checklist(self, task_id: int, item_ids: tuple[int, ...]) -> None:
        self._repository.reorder_checklist(task_id, item_ids)

    def work_logs(
        self,
        *,
        log_date: date | None = None,
        task_id: int | None = None,
    ) -> tuple[WorkLog, ...]:
        if task_id is not None:
            self._task_service.get(task_id)
        return self._repository.list_work_logs(log_date=log_date, task_id=task_id)

    def add_work_log(
        self,
        *,
        task_id: int | None,
        occurrence_id: int | None = None,
        log_date: date,
        content: str,
        result: str = "",
        now: datetime | None = None,
    ) -> WorkLog:
        priority = (
            self._task_service.get(task_id).priority
            if task_id is not None
            else TaskPriority.NORMAL
        )
        return self._repository.add_work_log(
            WorkLog.create(
                task_id=task_id,
                occurrence_id=occurrence_id,
                log_date=log_date,
                content=content,
                result=result,
                priority_snapshot=priority,
                now=now,
            )
        )

    def update_work_log(
        self,
        log_id: int,
        *,
        log_date: date,
        content: str,
        result: str,
        now: datetime | None = None,
    ) -> WorkLog:
        work_log = self._repository.get_work_log(log_id)
        if work_log is None:
            raise LookupError(f"업무일지 {log_id}을(를) 찾을 수 없습니다.")
        return self._repository.update_work_log(
            work_log.edit(log_date=log_date, content=content, result=result, now=now)
        )

    def delete_work_log(self, log_id: int) -> None:
        self._repository.delete_work_log(log_id)
