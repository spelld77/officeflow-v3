"""Calendar-day movement without changing unrelated task fields."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from officeflow.application.tasks import TaskDraft
from officeflow.domain.task import Task


def checked_local(value: datetime) -> datetime:
    stamp = value.astimezone(UTC)
    if stamp.astimezone(value.tzinfo).replace(tzinfo=None) != value.replace(tzinfo=None):
        raise ValueError("이 날짜에는 입력한 시각이 없습니다. 다른 시각을 지정하세요.")
    if value.utcoffset() != value.replace(fold=1 - value.fold).utcoffset():
        raise ValueError("이 날짜에는 시각이 두 번 있습니다. 중복되지 않는 시각을 지정하세요.")
    return stamp


def moved_schedule(
    starts_at: datetime | None,
    ends_at: datetime | None,
    *,
    target: date,
    timezone: str,
    all_day: bool,
    at: time | None = None,
) -> tuple[datetime, datetime | None]:
    zone = ZoneInfo(timezone)
    if starts_at is None:
        start = datetime.combine(target, at or time.min, zone)
        end = datetime.combine(target + timedelta(days=1), time.min, zone) if all_day else None
    else:
        old = starts_at.astimezone(zone)
        desired = datetime.combine(target, at or old.time().replace(tzinfo=None), zone)
        delta = desired.replace(tzinfo=None) - old.replace(tzinfo=None)
        start = desired
        end = ends_at.astimezone(zone) + delta if ends_at is not None else None
    return checked_local(start), checked_local(end) if end is not None else None


def schedule_draft(
    task: Task, start: datetime | None, end: datetime | None, all_day: bool
) -> TaskDraft:
    return TaskDraft(
        title=task.title,
        description=task.description,
        priority=task.priority,
        status=task.status,
        is_pinned=task.is_pinned,
        all_day=all_day,
        starts_at=start,
        ends_at=end,
        timezone=task.timezone,
        recurrence_rule=task.recurrence_rule,
    )
