"""Pure actual-duration day layout. Visual minimum height is never a saved duration."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from officeflow.application.reminder_groups import reminder_scope_key
from officeflow.application.reminder_preview import ReminderPreviewItem
from officeflow.application.tasks import ScheduledTask

DayValue = ScheduledTask | ReminderPreviewItem


def day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    return datetime.combine(day, time.min, zone).astimezone(UTC), datetime.combine(
        day + timedelta(days=1), time.min, zone
    ).astimezone(UTC)


def value_scope(value: DayValue) -> tuple[int | None, datetime | None]:
    if isinstance(value, ReminderPreviewItem):
        return reminder_scope_key(value)
    return value.id, value.occurrence_start if value.task.recurrence_rule else None


def alarm_key(item: ReminderPreviewItem) -> tuple[datetime, int, str, int, datetime]:
    return (
        item.effective_at,
        item.task.id or 0,
        item.occurrence_start.isoformat() if item.occurrence_start else "",
        item.reminder_id,
        item.scheduled_at,
    )


def day_alarms(items: tuple[ReminderPreviewItem, ...]) -> tuple[ReminderPreviewItem, ...]:
    return tuple(sorted({item.key: item for item in items}.values(), key=alarm_key))


@dataclass(frozen=True, slots=True)
class DayMark:
    values: tuple[DayValue, ...]
    minute: float
    bottom: float
    column: str
    lane: int = 0
    lanes: int = 1


def day_marks(
    day: date,
    timezone: str,
    schedules: tuple[ScheduledTask, ...],
    alarms: tuple[ReminderPreviewItem, ...],
) -> tuple[DayMark, ...]:
    lower, upper = day_bounds(day, timezone)
    day_minutes = (upper - lower).total_seconds() / 60
    visual_minutes = 38 * 60 / 56

    def minute(value: datetime) -> float:
        return max(0.0, min(day_minutes, (value.astimezone(UTC) - lower).total_seconds() / 60))

    rows: list[tuple[float, float, DayValue]] = []
    for task in schedules:
        if task.all_day:
            continue
        start = minute(task.starts_at)
        end = minute(task.ends_at) if task.ends_at is not None else start
        if start >= day_minutes or (
            task.ends_at is not None and task.ends_at.astimezone(UTC) <= lower
        ):
            continue
        rows.append((start, max(start + visual_minutes, end), task))
    rows.sort(key=lambda row: (row[0], row[1], row[2].task.id or 0))
    marks: list[DayMark] = []
    cluster: list[tuple[float, float, DayValue]] = []
    cluster_end = -1.0

    def flush() -> None:
        if not cluster:
            return
        ends: list[float] = []
        placed = []
        for start, end, value in cluster:
            lane = next((i for i, last in enumerate(ends) if last <= start), len(ends))
            if lane == len(ends):
                ends.append(end)
            else:
                ends[lane] = end
            placed.append((start, end, value, lane))
        lanes = min(3, len(ends))
        hidden = []
        for start, end, value, lane in placed:
            if len(ends) > 3 and lane >= 2:
                hidden.append((start, end, value))
            else:
                marks.append(
                    DayMark((value,), start, min(day_minutes, end), "schedule", lane, lanes)
                )
        if hidden:
            marks.append(
                DayMark(
                    tuple(row[2] for row in hidden),
                    min(row[0] for row in hidden),
                    min(day_minutes, max(row[1] for row in hidden)),
                    "schedule",
                    2,
                    3,
                )
            )

    for row in rows:
        if cluster and row[0] >= cluster_end:
            flush()
            cluster = []
            cluster_end = -1
        cluster.append(row)
        cluster_end = max(cluster_end, row[1])
    flush()
    alarm_cluster: list[DayValue] = []
    start = end = 0.0
    for alarm in day_alarms(alarms):
        if not (alarm.is_pending or alarm.needs_attention):
            continue
        current = minute(alarm.effective_at)
        if alarm_cluster and current >= end:
            marks.append(DayMark(tuple(alarm_cluster), start, min(day_minutes, end), "alarm"))
            alarm_cluster = []
        if not alarm_cluster:
            start = current
        alarm_cluster.append(alarm)
        end = current + visual_minutes
    if alarm_cluster:
        marks.append(DayMark(tuple(alarm_cluster), start, min(day_minutes, end), "alarm"))
    return tuple(marks)
