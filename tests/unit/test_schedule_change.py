from datetime import UTC, date, datetime, time, timedelta

import pytest

from officeflow.application.schedule_change import moved_schedule


def test_time_and_duration_preserved():
    start = datetime(2026, 10, 16, 3, 24, tzinfo=UTC)
    a, b = moved_schedule(
        start,
        start + timedelta(hours=2),
        target=date(2026, 10, 17),
        timezone="Asia/Seoul",
        all_day=False,
    )
    assert a == start + timedelta(days=1) and b == a + timedelta(hours=2)


def test_unscheduled_all_day_exclusive_end():
    a, b = moved_schedule(
        None, None, target=date(2026, 10, 17), timezone="Asia/Seoul", all_day=True
    )
    assert a == datetime(2026, 10, 16, 15, tzinfo=UTC) and b == a + timedelta(days=1)


@pytest.mark.parametrize(
    "day,at", [(date(2026, 3, 8), time(2, 30)), (date(2026, 11, 1), time(1, 30))]
)
def test_dst_rejected(day, at):
    with pytest.raises(ValueError):
        moved_schedule(None, None, target=day, timezone="America/New_York", all_day=False, at=at)
