"""Calendar-only read projection; reminder acknowledgements never complete a task."""

from datetime import UTC, datetime
from enum import IntEnum

from officeflow.application.reminder_groups import ReminderGroup
from officeflow.application.tasks import ScheduledTask
from officeflow.domain.enums import OccurrenceStatus, TaskStatus
from officeflow.domain.task import Task


class CalendarState(IntEnum):
    UNFINISHED = 0
    UNKNOWN = 1
    COMPLETED = 2
    INACTIVE = 3


def task_calendar_state(task: Task, occurrence: OccurrenceStatus | None = None) -> CalendarState:
    if task.deleted_at or task.status in {TaskStatus.CANCELED, TaskStatus.ARCHIVED}:
        return CalendarState.INACTIVE
    if task.status is TaskStatus.COMPLETED:
        return CalendarState.COMPLETED
    if occurrence in {OccurrenceStatus.CANCELED, OccurrenceStatus.SKIPPED}:
        return CalendarState.INACTIVE
    if occurrence is OccurrenceStatus.COMPLETED:
        return CalendarState.COMPLETED
    if task.status in {TaskStatus.ACTIVE, TaskStatus.PENDING}:
        return CalendarState.UNFINISHED
    return CalendarState.UNKNOWN


def calendar_state(row: ScheduledTask | ReminderGroup) -> CalendarState:
    if isinstance(row, ReminderGroup):
        if row.scheduled is not None:
            return calendar_state(row.scheduled)
        return task_calendar_state(row.task, row.representative.occurrence_status)
    return task_calendar_state(row.task, row.occurrence_status)


def calendar_row_key(row: ScheduledTask | ReminderGroup) -> tuple[int, datetime, int, datetime]:
    stamp = (
        (row.scheduled.starts_at if row.scheduled else row.representative.effective_at)
        if isinstance(row, ReminderGroup)
        else row.starts_at
    )
    return (
        calendar_state(row),
        stamp,
        row.id or 0,
        row.occurrence_start or datetime.min.replace(tzinfo=UTC),
    )
