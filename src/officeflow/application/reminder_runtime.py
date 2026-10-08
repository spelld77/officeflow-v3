"""Saved execution facts, independent of an editor's unsaved input."""

from dataclasses import dataclass
from datetime import datetime

from officeflow.domain.enums import OccurrenceStatus, TaskStatus
from officeflow.domain.reminder import Reminder, ReminderDelivery
from officeflow.domain.task import Task


def scope_label(task: Task, occurrence: OccurrenceStatus | None = None) -> str:
    if task.deleted_at:
        return "휴지통 업무"
    closed = {
        TaskStatus.COMPLETED: "업무 완료",
        TaskStatus.CANCELED: "업무 취소",
        TaskStatus.ARCHIVED: "업무 보관",
    }
    if task.status in closed:
        return closed[task.status]
    return (
        {
            OccurrenceStatus.COMPLETED: "이번 회차 완료",
            OccurrenceStatus.SKIPPED: "이번 회차 건너뜀",
            OccurrenceStatus.CANCELED: "이번 회차 취소",
        }.get(occurrence, "")
        if occurrence is not None
        else ""
    )


@dataclass(frozen=True, slots=True)
class ReminderRuntimeState:
    task: Task
    occurrences: tuple[tuple[datetime, OccurrenceStatus], ...]
    deliveries: tuple[ReminderDelivery, ...]
    reminders: tuple[Reminder, ...]
    read_at: datetime

    def occurrence_status(self, start: datetime | None) -> OccurrenceStatus | None:
        return next((status for key, status in self.occurrences if key == start), None)
