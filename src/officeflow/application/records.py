from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from officeflow.application.tasks import TaskService
from officeflow.domain.enums import TaskPriority
from officeflow.domain.records import ChecklistItem, WorkLog


class DuplicateWorkLogError(ValueError):
    """Raised before saving an exact duplicate work log."""


@dataclass(frozen=True, slots=True)
class WorkLogPage:
    items: tuple[WorkLog, ...]
    total: int
    offset: int
    limit: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


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
        search: str = "",
        occurrence_id: int | None = None,
        occurrence_only: bool = False,
        limit: int | None = None,
    ) -> tuple[WorkLog, ...]: ...

    def query_work_logs(
        self,
        *,
        search: str = "",
        date_from: date | None = None,
        date_to: date | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> WorkLogPage: ...

    def has_duplicate_work_log(
        self,
        *,
        task_id: int | None,
        log_date: date,
        content: str,
        exclude_log_id: int | None = None,
    ) -> bool: ...

    def count_work_logs(self, task_ids: tuple[int, ...]) -> dict[int, int]: ...

    def add_work_log(self, work_log: WorkLog) -> WorkLog: ...

    def update_work_log(self, work_log: WorkLog) -> WorkLog: ...

    def delete_work_log(self, log_id: int) -> None: ...


class RecordService:
    def __init__(self, repository: RecordRepository, task_service: TaskService) -> None:
        self._repository = repository
        self._task_service = task_service

    def checklist_for_task(self, task_id: int) -> tuple[ChecklistItem, ...]:
        self._task_service.get_including_deleted(task_id)
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
        search: str = "",
        occurrence_start: datetime | None = None,
        occurrence_only: bool = False,
        limit: int | None = None,
    ) -> tuple[WorkLog, ...]:
        if task_id is not None:
            self._task_service.get_including_deleted(task_id)
        occurrence_id: int | None = None
        if occurrence_only:
            if task_id is None or occurrence_start is None:
                raise ValueError("반복 발생 건을 조회하려면 업무와 발생 시각이 필요합니다.")
            occurrence = self._task_service.occurrence(task_id, occurrence_start)
            if occurrence is None or occurrence.id is None:
                return ()
            occurrence_id = occurrence.id
        if limit is not None and not 1 <= limit <= 500:
            raise ValueError("한 번에 조회할 업무일지는 1~500개여야 합니다.")
        return self._repository.list_work_logs(
            log_date=log_date,
            task_id=task_id,
            search=search,
            occurrence_id=occurrence_id,
            occurrence_only=occurrence_only,
            limit=limit,
        )

    def work_log_page(
        self,
        *,
        search: str = "",
        date_from: date | None = None,
        date_to: date | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> WorkLogPage:
        if offset < 0:
            raise ValueError("조회 시작 위치는 0 이상이어야 합니다.")
        if not 1 <= limit <= 100:
            raise ValueError("한 번에 조회할 업무일지는 1~100개여야 합니다.")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ValueError("검색 시작일은 종료일보다 늦을 수 없습니다.")
        return self._repository.query_work_logs(
            search=search,
            date_from=date_from,
            date_to=date_to,
            offset=offset,
            limit=limit,
        )

    def work_log_counts(self, task_ids: tuple[int, ...]) -> dict[int, int]:
        if not task_ids:
            return {}
        self._task_service.get_many_including_deleted(task_ids)
        return self._repository.count_work_logs(task_ids)

    def add_work_log(
        self,
        *,
        task_id: int | None,
        occurrence_id: int | None = None,
        occurrence_start: datetime | None = None,
        log_date: date,
        content: str,
        result: str = "",
        now: datetime | None = None,
        allow_duplicate: bool = False,
    ) -> WorkLog:
        if occurrence_id is not None and occurrence_start is not None:
            raise ValueError("반복 발생 ID와 발생 시각은 동시에 지정할 수 없습니다.")
        if occurrence_start is not None:
            if task_id is None:
                raise ValueError("반복 발생 업무일지에는 업무가 필요합니다.")
            occurrence = self._task_service.ensure_occurrence(task_id, occurrence_start)
            occurrence_id = occurrence.id
        priority = (
            self._task_service.get(task_id).priority
            if task_id is not None
            else TaskPriority.NORMAL
        )
        if not allow_duplicate and self._repository.has_duplicate_work_log(
            task_id=task_id,
            log_date=log_date,
            content=content.strip(),
        ):
            raise DuplicateWorkLogError("같은 날짜와 내용의 업무일지가 이미 있습니다.")
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
        allow_duplicate: bool = False,
    ) -> WorkLog:
        work_log = self._repository.get_work_log(log_id)
        if work_log is None:
            raise LookupError(f"업무일지 {log_id}을(를) 찾을 수 없습니다.")
        if not allow_duplicate and self._repository.has_duplicate_work_log(
            task_id=work_log.task_id,
            log_date=log_date,
            content=content.strip(),
            exclude_log_id=log_id,
        ):
            raise DuplicateWorkLogError("같은 날짜와 내용의 업무일지가 이미 있습니다.")
        return self._repository.update_work_log(
            work_log.edit(log_date=log_date, content=content, result=result, now=now)
        )

    def delete_work_log(self, log_id: int) -> None:
        self._repository.delete_work_log(log_id)
