from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from officeflow.domain.enums import TaskStatus
from officeflow.domain.task import InvalidStatusTransitionError, Task, TaskValidationError

NOW = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)


def test_task_rejects_blank_title() -> None:
    with pytest.raises(TaskValidationError, match="제목"):
        Task.create(title="   ", now=NOW)


def test_task_rejects_end_before_start() -> None:
    with pytest.raises(TaskValidationError, match="종료"):
        Task.create(
            title="잘못된 일정",
            starts_at=NOW,
            ends_at=NOW - timedelta(hours=1),
            now=NOW,
        )


def test_task_requires_timezone_aware_schedule() -> None:
    with pytest.raises(TaskValidationError, match="시간대"):
        Task.create(title="시간대 없는 일정", starts_at=datetime(2026, 9, 12, 9), now=NOW)


def test_completing_task_records_completion_time() -> None:
    task = Task.create(title="완료할 업무", now=NOW)

    completed = task.transition_to(TaskStatus.COMPLETED, now=NOW + timedelta(hours=1))

    assert completed.status is TaskStatus.COMPLETED
    assert completed.completed_at == NOW + timedelta(hours=1)


def test_completed_task_cannot_transition_directly_to_pending() -> None:
    completed = Task.create(title="완료 업무", status=TaskStatus.COMPLETED, now=NOW)

    with pytest.raises(InvalidStatusTransitionError):
        completed.transition_to(TaskStatus.PENDING, now=NOW + timedelta(hours=1))
