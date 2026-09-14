from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from officeflow.domain.recurrence import (
    RecurrenceFrequency,
    RecurrenceSpec,
    RecurrenceValidationError,
    expand_recurrence,
    next_recurrence_start,
    parse_simple_recurrence,
    validate_recurrence_rule,
)


def test_simple_rule_round_trip_with_inclusive_local_until() -> None:
    spec = RecurrenceSpec(
        RecurrenceFrequency.DAILY,
        interval=2,
        until=date(2026, 9, 20),
    )

    rule = spec.to_rrule("Asia/Seoul")
    restored = parse_simple_recurrence(rule, "Asia/Seoul")

    assert rule == "FREQ=DAILY;INTERVAL=2;UNTIL=20260920T145959Z"
    assert restored == spec


def test_daily_recurrence_expands_only_overlapping_occurrences() -> None:
    starts = expand_recurrence(
        "FREQ=DAILY;INTERVAL=1",
        template_start=datetime(2026, 9, 12, 15, 0, tzinfo=UTC),
        template_end=datetime(2026, 9, 13, 15, 0, tzinfo=UTC),
        timezone="Asia/Seoul",
        range_start=datetime(2026, 9, 14, 15, 0, tzinfo=UTC),
        range_end=datetime(2026, 9, 17, 15, 0, tzinfo=UTC),
    )

    assert [start for start, _end in starts] == [
        datetime(2026, 9, 14, 15, 0, tzinfo=UTC),
        datetime(2026, 9, 15, 15, 0, tzinfo=UTC),
        datetime(2026, 9, 16, 15, 0, tzinfo=UTC),
    ]


def test_weekly_recurrence_preserves_wall_clock_across_dst() -> None:
    zone = ZoneInfo("America/New_York")
    template = datetime(2026, 10, 25, 9, 0, tzinfo=zone).astimezone(UTC)

    following = next_recurrence_start(
        "FREQ=WEEKLY;INTERVAL=1",
        template_start=template,
        timezone="America/New_York",
        after=template,
    )

    assert following is not None
    assert following.astimezone(zone).date() == date(2026, 11, 1)
    assert following.astimezone(zone).hour == 9


def test_invalid_rule_is_rejected() -> None:
    with pytest.raises(RecurrenceValidationError):
        validate_recurrence_rule(
            "FREQ=UNKNOWN",
            datetime(2026, 9, 13, tzinfo=UTC),
        )
