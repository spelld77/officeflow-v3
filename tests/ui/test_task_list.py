from __future__ import annotations

from datetime import UTC, datetime, timedelta

from PySide6.QtCore import QModelIndex
from PySide6.QtWidgets import QStyleOptionViewItem

from officeflow.application.tasks import TaskGroup, TaskPage
from officeflow.domain.enums import TaskStatus
from officeflow.domain.task import Task
from officeflow.presentation.task_list import (
    GroupHeader,
    LoadMoreRow,
    TaskItemDelegate,
    TaskListModel,
)

NOW = datetime(2026, 9, 13, 3, 0, tzinfo=UTC)


def make_task(index: int, *, status: TaskStatus = TaskStatus.ACTIVE) -> Task:
    return Task.create(
        title=f"업무 {index:03d}",
        status=status,
        starts_at=NOW + timedelta(minutes=index),
        ends_at=NOW + timedelta(minutes=index + 30),
        now=NOW,
    )


def test_flat_model_fetches_fifty_tasks_at_a_time() -> None:
    tasks = tuple(make_task(index) for index in range(120))
    model = TaskListModel()
    offsets: list[int] = []

    def load(offset: int, limit: int) -> TaskPage:
        offsets.append(offset)
        return TaskPage(tasks[offset : offset + limit], len(tasks), offset, limit)

    model.set_page(TaskPage(tasks[:50], 120, 0, 50), load)

    assert model.rowCount() == 50
    assert model.canFetchMore()
    model.fetchMore(QModelIndex())
    assert offsets == [50]
    assert model.rowCount() == 100
    assert model.loaded_task_count == 100
    assert model.total_task_count == 120


def test_group_model_collapses_completed_and_loads_a_group_page() -> None:
    overdue = tuple(make_task(index) for index in range(60))
    pages = {
        TaskGroup.OVERDUE: TaskPage(overdue[:50], 60, 0, 50),
        TaskGroup.IN_PROGRESS: TaskPage((), 0, 0, 50),
        TaskGroup.UPCOMING: TaskPage((), 0, 0, 50),
        TaskGroup.COMPLETED: TaskPage((make_task(99, status=TaskStatus.COMPLETED),), 1, 0, 50),
    }
    model = TaskListModel()

    def load(group: TaskGroup, offset: int, limit: int) -> TaskPage:
        assert group is TaskGroup.OVERDUE
        return TaskPage(overdue[offset : offset + limit], len(overdue), offset, limit)

    model.set_group_pages(
        pages,
        collapsed=frozenset({TaskGroup.COMPLETED}),
        loader=load,
    )

    completed_header = model.entry_at(model.index_for_group(TaskGroup.COMPLETED))
    assert isinstance(completed_header, GroupHeader)
    assert completed_header.collapsed is True
    load_more_index = next(
        model.index(row, 0)
        for row in range(model.rowCount())
        if isinstance(model.entry_at(model.index(row, 0)), LoadMoreRow)
    )
    load_more = model.entry_at(load_more_index)
    assert isinstance(load_more, LoadMoreRow)
    model.load_more(load_more.group)
    assert model.loaded_task_count == 61
    assert not any(
        isinstance(model.entry_at(model.index(row, 0)), LoadMoreRow)
        for row in range(model.rowCount())
    )


def test_compact_delegate_reduces_task_row_height() -> None:
    model = TaskListModel()
    model.set_tasks([make_task(1)])
    delegate = TaskItemDelegate()
    option = QStyleOptionViewItem()
    index = model.index(0, 0)

    assert delegate.sizeHint(option, index).height() == 70
    delegate.set_compact(True)
    assert delegate.sizeHint(option, index).height() == 48
