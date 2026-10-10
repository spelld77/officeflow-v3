"""Bounded, read-only display grouping; reminder identity and writes stay independent."""

from dataclasses import dataclass, replace
from datetime import datetime

from officeflow.application.reminder_preview import ReminderPreviewItem
from officeflow.application.tasks import ScheduledTask
from officeflow.domain.task import Task

ScopeKey = tuple[int | None, datetime | None]


def reminder_scope_key(item: ReminderPreviewItem) -> ScopeKey:
    return item.task.id, item.occurrence_start if item.task.recurrence_rule else None


def _attention_rank(item: ReminderPreviewItem) -> tuple[int, datetime, int]:
    rank = (
        0
        if item.is_pending and item.status == "미확인"
        else (1 if item.is_pending else 2 if item.needs_attention else 3)
    )
    return rank, item.effective_at, item.reminder_id


@dataclass(frozen=True, slots=True)
class ReminderGroup:
    key: ScopeKey
    items: tuple[ReminderPreviewItem, ...]
    representative: ReminderPreviewItem
    pending_count: int
    attention_count: int
    scheduled: ScheduledTask | None = None

    @property
    def task(self) -> Task:
        return self.scheduled.task if self.scheduled else self.representative.task

    @property
    def id(self) -> int | None:
        return self.key[0]

    @property
    def occurrence_start(self) -> datetime | None:
        return self.key[1]


def group_reminders(items: tuple[ReminderPreviewItem, ...]) -> tuple[ReminderGroup, ...]:
    grouped: dict[ScopeKey, dict[tuple[int, datetime | None, datetime], ReminderPreviewItem]] = {}
    for item in items:
        grouped.setdefault(reminder_scope_key(item), {})[item.key] = item
    groups = []
    for key, values in grouped.items():
        alarms = tuple(sorted(values.values(), key=lambda i: (i.effective_at, i.reminder_id)))
        groups.append(
            ReminderGroup(
                key,
                alarms,
                min(alarms, key=_attention_rank),
                sum(i.is_pending for i in alarms),
                sum(i.needs_attention for i in alarms),
            )
        )
    return tuple(sorted(groups, key=lambda g: (*_attention_rank(g.representative), g.id or 0)))


def merge_day_reminders(
    schedules: tuple[ScheduledTask, ...], alarms: tuple[ReminderPreviewItem, ...]
) -> tuple[ScheduledTask | ReminderGroup, ...]:
    groups = {group.key: group for group in group_reminders(alarms)}
    rows: list[ScheduledTask | ReminderGroup] = []
    seen: set[ScopeKey] = set()
    for schedule in schedules:
        key = (schedule.id, schedule.occurrence_start if schedule.task.recurrence_rule else None)
        if key in seen:
            continue
        seen.add(key)
        group = groups.pop(key, None)
        rows.append(replace(group, scheduled=schedule) if group else schedule)
    rows.extend(groups.values())
    return tuple(rows)
