from datetime import UTC, date, datetime, timedelta

from officeflow.application.day_display import day_alarms, day_marks
from officeflow.application.reminder_preview import ReminderPreviewItem
from officeflow.application.tasks import ScheduledTask
from officeflow.domain.task import Task


def test_dense_schedule_lanes_keep_all_items_without_inventing_end_times():
    start = datetime(2026, 10, 15, 1, tzinfo=UTC)
    schedules = tuple(
        ScheduledTask(Task.create(title=f"일정{i}", starts_at=start), start, None) for i in range(8)
    )
    marks = day_marks(date(2026, 10, 15), "Asia/Seoul", schedules, ())
    assert max(mark.lanes for mark in marks) == 3
    assert sum(len(mark.values) for mark in marks) == 8
    assert any(len(mark.values) > 1 for mark in marks)
    assert all(mark.minute == 600 for mark in marks)
    assert all(item.ends_at is None for item in schedules)


def test_alarms_are_separate_from_schedule_and_completed_not_pending():
    start = datetime(2026, 10, 15, 5, tzinfo=UTC)
    task = Task.create(title="제출", starts_at=start)
    alarms = tuple(
        ReminderPreviewItem(
            task, i, None, start - timedelta(hours=i), start - timedelta(hours=i), "예정"
        )
        for i in (1, 2)
    )
    marks = day_marks(date(2026, 10, 15), "UTC", (ScheduledTask(task, start, None),), alarms)
    assert sum(mark.column == "schedule" for mark in marks) == 1
    assert sum(len(mark.values) for mark in marks if mark.column == "alarm") == 2
    assert len(day_alarms((*alarms, alarms[0]))) == 2


def test_overnight_schedule_clips_only_the_visible_geometry():
    start = datetime(2026, 10, 14, 23, tzinfo=UTC)
    end = start + timedelta(hours=3)
    task = Task.create(title="야간", starts_at=start, ends_at=end)
    marks = day_marks(date(2026, 10, 15), "UTC", (ScheduledTask(task, start, end),), ())
    assert marks[0].minute == 0 and marks[0].bottom == 120
    assert marks[0].values[0].starts_at == start


def test_dst_repeated_hour_has_real_duration_and_distinct_positions():
    from zoneinfo import ZoneInfo

    from officeflow.application.day_display import day_bounds

    zone = ZoneInfo("America/New_York")
    day = date(2026, 11, 1)
    lower, upper = day_bounds(day, zone.key)
    assert (upper - lower).total_seconds() == 25 * 3600
    start = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0).astimezone(UTC)
    end = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1).astimezone(UTC)
    task = Task.create(title="중복 시각", starts_at=start, ends_at=end)
    marks = day_marks(day, zone.key, (ScheduledTask(task, start, end),), ())
    assert marks[0].bottom - marks[0].minute == 60
