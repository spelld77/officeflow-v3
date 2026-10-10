from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from officeflow.presentation.input_controls import default_start_at, parse_time_text


@pytest.mark.parametrize(
    "text,expected",
    [
        ("11", "11:00"),
        ("1124", "11:24"),
        ("11:24", "11:24"),
        ("924", "09:24"),
        ("0", "00:00"),
        ("2359", "23:59"),
    ],
)
def test_whole_time_parser(text, expected):
    assert parse_time_text(text).strftime("%H:%M") == expected


@pytest.mark.parametrize("text", ["", "2400", "1160", "12:99", "112499", "abc", "-1", "1:2:3"])
def test_invalid_time_is_not_corrected(text):
    with pytest.raises(ValueError):
        parse_time_text(text)


@pytest.mark.parametrize(
    "now,expected",
    [
        ("2026-10-10T14:00:00", "2026-10-10T15:00:00"),
        ("2026-10-10T14:00:01", "2026-10-10T16:00:00"),
        ("2026-10-10T14:10:00", "2026-10-10T16:00:00"),
        ("2026-12-31T23:58:00", "2027-01-01T01:00:00"),
    ],
)
def test_default_is_at_least_one_hour_later_on_the_hour(now, expected):
    zone = ZoneInfo("Asia/Seoul")
    assert default_start_at(
        datetime.fromisoformat(now).replace(tzinfo=zone)
    ) == datetime.fromisoformat(expected).replace(tzinfo=zone)


@pytest.mark.parametrize(
    "now", [datetime(2026, 3, 8, 1, 30), datetime(2026, 11, 1, 0, 0), datetime(2026, 11, 1, 1, 30)]
)
def test_dst_default_has_real_one_hour_margin_and_representable_fold(now):
    now = now.replace(tzinfo=ZoneInfo("America/New_York"))
    result = default_start_at(now)
    assert result.astimezone(UTC) - now.astimezone(UTC) >= timedelta(hours=1)
    assert result.minute == result.second == result.microsecond == 0
    assert result.fold == 0
    assert result == result.astimezone(UTC).astimezone(result.tzinfo)
