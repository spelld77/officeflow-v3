from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from officeflow.application.records import (
    DuplicateWorkLogError,
    RecordRepository,
    RecordService,
    WorkLogPage,
)
from officeflow.application.tasks import TaskDraft, TaskService
from officeflow.domain.enums import TaskPriority
from officeflow.domain.records import ChecklistItem, RecordValidationError, WorkLog
from tests.unit.test_task_service import InMemoryTaskRepository


class InMemoryRecordRepository(RecordRepository):
    def __init__(self) -> None:
        self.checklist: dict[int, ChecklistItem] = {}
        self.work_log_items: dict[int, WorkLog] = {}
        self.next_checklist_id = 1
        self.next_log_id = 1

    def list_checklist(self, task_id: int) -> tuple[ChecklistItem, ...]:
        return tuple(
            sorted(
                (item for item in self.checklist.values() if item.task_id == task_id),
                key=lambda item: (item.position, item.id or 0),
            )
        )

    def add_checklist_item(self, item: ChecklistItem) -> ChecklistItem:
        saved = replace(item, id=self.next_checklist_id)
        self.checklist[self.next_checklist_id] = saved
        self.next_checklist_id += 1
        return saved

    def update_checklist_item(self, item: ChecklistItem) -> ChecklistItem:
        assert item.id is not None
        self.checklist[item.id] = item
        return item

    def delete_checklist_item(self, item_id: int) -> None:
        del self.checklist[item_id]

    def reorder_checklist(self, task_id: int, item_ids: tuple[int, ...]) -> None:
        for position, item_id in enumerate(item_ids):
            self.checklist[item_id] = replace(self.checklist[item_id], position=position)

    def get_work_log(self, log_id: int) -> WorkLog | None:
        return self.work_log_items.get(log_id)

    def list_work_logs(
        self,
        *,
        log_date: date | None = None,
        task_id: int | None = None,
        search: str = "",
        occurrence_id: int | None = None,
        occurrence_only: bool = False,
        limit: int | None = None,
    ) -> tuple[WorkLog, ...]:
        normalized = search.strip().casefold()
        items = tuple(
            sorted(
                (
                    item
                    for item in self.work_log_items.values()
                    if (log_date is None or item.log_date == log_date)
                    and (task_id is None or item.task_id == task_id)
                    and (not occurrence_only or item.occurrence_id == occurrence_id)
                    and (
                        not normalized
                        or normalized in f"{item.content}\n{item.result}".casefold()
                    )
                ),
                key=lambda item: (item.log_date, item.updated_at),
                reverse=True,
            )
        )
        return items[:limit] if limit is not None else items

    def query_work_logs(
        self,
        *,
        search: str = "",
        date_from: date | None = None,
        date_to: date | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> WorkLogPage:
        items = tuple(
            item
            for item in self.list_work_logs(search=search)
            if (date_from is None or item.log_date >= date_from)
            and (date_to is None or item.log_date <= date_to)
        )
        return WorkLogPage(
            items=items[offset : offset + limit],
            total=len(items),
            offset=offset,
            limit=limit,
        )

    def has_duplicate_work_log(
        self,
        *,
        task_id: int | None,
        log_date: date,
        content: str,
        exclude_log_id: int | None = None,
    ) -> bool:
        return any(
            item.task_id == task_id
            and item.log_date == log_date
            and item.content == content
            and item.id != exclude_log_id
            for item in self.work_log_items.values()
        )

    def count_work_logs(self, task_ids: tuple[int, ...]) -> dict[int, int]:
        return {
            task_id: sum(
                1 for item in self.work_log_items.values() if item.task_id == task_id
            )
            for task_id in task_ids
        }

    def add_work_log(self, work_log: WorkLog) -> WorkLog:
        saved = replace(work_log, id=self.next_log_id)
        self.work_log_items[self.next_log_id] = saved
        self.next_log_id += 1
        return saved

    def update_work_log(self, work_log: WorkLog) -> WorkLog:
        assert work_log.id is not None
        self.work_log_items[work_log.id] = work_log
        return work_log

    def delete_work_log(self, log_id: int) -> None:
        del self.work_log_items[log_id]


def make_services() -> tuple[TaskService, RecordService, InMemoryRecordRepository]:
    task_service = TaskService(InMemoryTaskRepository())
    repository = InMemoryRecordRepository()
    return task_service, RecordService(repository, task_service), repository


def test_checklist_add_complete_and_reorder() -> None:
    task_service, service, _repository = make_services()
    task = task_service.create(TaskDraft(title="릴리스 준비"))
    assert task.id is not None
    first = service.add_checklist_item(task.id, "  테스트 통과  ")
    second = service.add_checklist_item(task.id, "문서 갱신")
    assert first.id is not None and second.id is not None
    completed_at = datetime(2026, 9, 15, 1, 0, tzinfo=UTC)

    completed = service.set_checklist_done(first, True, now=completed_at)
    service.reorder_checklist(task.id, (second.id, first.id))

    items = service.checklist_for_task(task.id)
    assert [item.content for item in items] == ["문서 갱신", "테스트 통과"]
    assert completed.completed_at == completed_at


def test_blank_checklist_and_work_log_are_rejected() -> None:
    task_service, service, _repository = make_services()
    task = task_service.create(TaskDraft(title="검증"))
    assert task.id is not None

    with pytest.raises(RecordValidationError):
        service.add_checklist_item(task.id, "   ")
    with pytest.raises(RecordValidationError):
        service.add_work_log(task_id=task.id, log_date=date(2026, 9, 15), content="")


def test_work_log_page_applies_date_range_and_pagination() -> None:
    task_service, service, _repository = make_services()
    task = task_service.create(TaskDraft(title="검색 업무"))
    assert task.id is not None
    for day in range(1, 31):
        service.add_work_log(
            task_id=task.id,
            log_date=date(2026, 8, day),
            content=f"검색 기록 {day}",
        )

    first = service.work_log_page(
        search="검색",
        date_from=date(2026, 8, 10),
        date_to=date(2026, 8, 30),
        limit=10,
    )
    second = service.work_log_page(
        search="검색",
        date_from=date(2026, 8, 10),
        date_to=date(2026, 8, 30),
        offset=10,
        limit=10,
    )

    assert first.total == 21
    assert len(first.items) == 10
    assert first.has_more
    assert len(second.items) == 10


def test_work_log_keeps_priority_snapshot_and_can_be_edited() -> None:
    task_service, service, _repository = make_services()
    task = task_service.create(
        TaskDraft(title="고객 미팅", priority=TaskPriority.IMPORTANT)
    )
    assert task.id is not None
    created_at = datetime(2026, 9, 15, 2, 0, tzinfo=UTC)
    work_log = service.add_work_log(
        task_id=task.id,
        log_date=date(2026, 9, 15),
        content="요구사항 정리",
        result="초안 완료",
        now=created_at,
    )
    assert work_log.id is not None

    updated = service.update_work_log(
        work_log.id,
        log_date=date(2026, 9, 16),
        content="요구사항 확정",
        result="승인 완료",
        now=datetime(2026, 9, 16, 2, 0, tzinfo=UTC),
    )

    assert updated.priority_snapshot is TaskPriority.IMPORTANT
    assert updated.content == "요구사항 확정"
    assert service.work_logs(log_date=date(2026, 9, 15)) == ()
    assert service.work_logs(log_date=date(2026, 9, 16)) == (updated,)


def test_duplicate_work_log_requires_explicit_confirmation() -> None:
    task_service, service, _repository = make_services()
    task = task_service.create(TaskDraft(title="중복 점검"))
    assert task.id is not None
    service.add_work_log(
        task_id=task.id,
        log_date=date(2026, 9, 15),
        content="  동일한 처리 내용  ",
    )

    with pytest.raises(DuplicateWorkLogError):
        service.add_work_log(
            task_id=task.id,
            log_date=date(2026, 9, 15),
            content="동일한 처리 내용",
        )

    duplicate = service.add_work_log(
        task_id=task.id,
        log_date=date(2026, 9, 15),
        content="동일한 처리 내용",
        allow_duplicate=True,
    )
    assert duplicate.id is not None


def test_work_log_count_is_returned_per_task() -> None:
    task_service, service, _repository = make_services()
    first = task_service.create(TaskDraft(title="첫 업무"))
    second = task_service.create(TaskDraft(title="둘째 업무"))
    assert first.id is not None and second.id is not None
    for index in range(2):
        service.add_work_log(
            task_id=first.id,
            log_date=date(2026, 9, 15 + index),
            content=f"첫 업무 기록 {index}",
        )
    service.add_work_log(
        task_id=second.id,
        log_date=date(2026, 9, 15),
        content="둘째 업무 기록",
    )

    assert service.work_log_counts((first.id, second.id)) == {
        first.id: 2,
        second.id: 1,
    }


def test_task_result_note_is_saved_without_changing_status() -> None:
    task_service, _service, _repository = make_services()
    task = task_service.create(TaskDraft(title="결과 기록"))
    assert task.id is not None

    saved = task_service.update_result_note(task.id, "  검토 완료  ")

    assert saved == "검토 완료"
    assert task_service.get(task.id).result_note == "검토 완료"


def test_recurring_result_note_is_kept_on_the_selected_occurrence() -> None:
    task_service, _service, _repository = make_services()
    occurrence_start = datetime(2026, 9, 15, 1, 0, tzinfo=UTC)
    task = task_service.create(
        TaskDraft(
            title="매일 확인",
            starts_at=occurrence_start,
            recurrence_rule="FREQ=DAILY;INTERVAL=1",
        )
    )
    assert task.id is not None

    saved = task_service.update_result_note(
        task.id,
        "오늘 확인 완료",
        occurrence_start=occurrence_start,
    )

    assert saved == "오늘 확인 완료"
    assert task_service.get(task.id).result_note == ""
    assert task_service.result_note(task.id, occurrence_start) == "오늘 확인 완료"


def test_recurring_work_logs_are_linked_and_filtered_by_occurrence() -> None:
    task_service, service, _repository = make_services()
    first_start = datetime(2026, 9, 15, 1, 0, tzinfo=UTC)
    second_start = datetime(2026, 9, 16, 1, 0, tzinfo=UTC)
    task = task_service.create(
        TaskDraft(
            title="매일 점검",
            starts_at=first_start,
            recurrence_rule="FREQ=DAILY;INTERVAL=1",
        )
    )
    assert task.id is not None

    first = service.add_work_log(
        task_id=task.id,
        occurrence_start=first_start,
        log_date=date(2026, 9, 15),
        content="첫째 날 점검",
    )
    second = service.add_work_log(
        task_id=task.id,
        occurrence_start=second_start,
        log_date=date(2026, 9, 16),
        content="둘째 날 점검",
    )

    assert first.occurrence_id is not None
    assert second.occurrence_id is not None
    assert first.occurrence_id != second.occurrence_id
    assert service.work_logs(
        task_id=task.id,
        occurrence_start=first_start,
        occurrence_only=True,
    ) == (first,)


def test_completing_with_result_uses_one_repository_update() -> None:
    repository = InMemoryTaskRepository()
    service = TaskService(repository)
    task = service.create(TaskDraft(title="결과와 함께 완료"))
    assert task.id is not None

    completed = service.complete(task.id, result_note="검수 완료")

    assert repository.update_calls == 1
    assert completed.status.value == "completed"
    assert completed.result_note == "검수 완료"
