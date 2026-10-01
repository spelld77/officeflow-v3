from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


def time_minutes(value: str) -> int:
    if (
        not isinstance(value, str)
        or len(value) != 5
        or value[2] != ":"
        or not value[:2].isascii()
        or not value[:2].isdigit()
        or not value[3:].isascii()
        or not value[3:].isdigit()
    ):
        raise ValueError("제외 시각은 HH:mm 형식이어야 합니다.")
    try:
        hour, minute = int(value[:2]), int(value[3:])
    except ValueError as error:
        raise ValueError("제외 시각은 HH:mm 형식이어야 합니다.") from error
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("유효하지 않은 제외 시각입니다.")
    return hour * 60 + minute


def exclusion_intervals(ranges: tuple[tuple[str, str], ...]) -> tuple[tuple[int, int], ...]:
    if len(ranges) > 8:
        raise ValueError("추가 제외 시간은 최대 8개입니다.")
    intervals: list[tuple[int, int]] = []
    for start, end in ranges:
        a, b = time_minutes(start), time_minutes(end)
        if a == b:
            raise ValueError("제외 시작과 끝은 달라야 합니다.")
        intervals.extend(((a, b),) if a < b else ((a, 1440), (0, b)))
    merged: list[tuple[int, int]] = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return tuple(merged)


@dataclass(frozen=True, slots=True)
class HourlySettings:
    timezone: str = "Asia/Seoul"
    minute: int = 35
    message: str = "인사랑 업무기록을 확인하세요."
    weekday_exclusion_start: str = "09:00"
    weekday_exclusion_end: str = "18:00"
    extra_exclusions: tuple[tuple[str, str], ...] = ()
    sound: bool = False
    morning_continuation_required: bool = True
    holiday_date: str | None = None
    morning_continuation_date: str | None = None

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)
        if type(self.minute) is not int or not 0 <= self.minute <= 59:
            raise ValueError("알림 분은 0~59 사이 정수여야 합니다.")
        if not isinstance(self.message, str) or not 1 <= len(self.message.strip()) <= 200:
            raise ValueError("알림 문구는 1~200자여야 합니다.")
        exclusion_intervals(((self.weekday_exclusion_start, self.weekday_exclusion_end),))
        exclusion_intervals(self.extra_exclusions)
        if type(self.sound) is not bool or type(self.morning_continuation_required) is not bool:
            raise ValueError("알림 옵션은 참/거짓 값이어야 합니다.")
        for value in (self.holiday_date, self.morning_continuation_date):
            if value is not None and (
                not isinstance(value, str) or date.fromisoformat(value).isoformat() != value
            ):
                raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.")


@dataclass(frozen=True, slots=True)
class HourBucket:
    key: str
    starts_at: datetime
    ends_at: datetime


def local_time(now: datetime, settings: HourlySettings) -> datetime:
    if now.tzinfo is None:
        raise ValueError("시간대가 있는 시각이 필요합니다.")
    return now.astimezone(ZoneInfo(settings.timezone))


def hour_bucket(now: datetime, settings: HourlySettings) -> HourBucket:
    local = local_time(now, settings)
    start = local.replace(minute=0, second=0, microsecond=0)
    end = (start.astimezone(UTC) + timedelta(hours=1)).astimezone(local.tzinfo)
    return HourBucket(start.isoformat(), start, end)


def exclusion_reason(
    now: datetime, settings: HourlySettings, *, active_since: datetime | None = None
) -> str | None:
    local = local_time(now, settings)
    minute = local.hour * 60 + local.minute
    if any(a <= minute < b for a, b in exclusion_intervals(settings.extra_exclusions)):
        return "extra_exclusion"
    if local.weekday() >= 5 or settings.holiday_date == local.date().isoformat():
        return None
    weekday_ranges = exclusion_intervals(
        ((settings.weekday_exclusion_start, settings.weekday_exclusion_end),)
    )
    if any(a <= minute < b for a, b in weekday_ranges):
        return "weekday_exclusion"
    if local.hour == 8 and settings.morning_continuation_required:
        threshold = local.replace(hour=8, minute=0, second=0, microsecond=0)
        continued = settings.morning_continuation_date == local.date().isoformat()
        continued |= active_since is not None and active_since.astimezone(
            UTC
        ) < threshold.astimezone(UTC)
        if not continued:
            return "morning_without_continuation"
    return None


def regular_at(bucket: HourBucket, settings: HourlySettings) -> datetime:
    return bucket.starts_at.replace(minute=settings.minute)


def regular_candidates(now: datetime, settings: HourlySettings) -> list[datetime]:
    """Build local clock-hour candidates; reject DST gaps and distinguish both folds."""
    local = local_time(now, settings)
    zone = ZoneInfo(settings.timezone)
    lower = now.astimezone(UTC)
    upper = lower + timedelta(hours=48)
    candidates: set[datetime] = set()
    for day_offset in range(-1, 4):
        day = local.date() + timedelta(days=day_offset)
        for hour in range(24):
            for fold in (0, 1):
                candidate = datetime(
                    day.year, day.month, day.day, hour, settings.minute, tzinfo=zone, fold=fold
                )
                utc = candidate.astimezone(UTC)
                roundtrip = utc.astimezone(zone)
                if roundtrip.replace(tzinfo=None) != candidate.replace(tzinfo=None):
                    continue
                if lower < utc <= upper:
                    candidates.add(utc)
    return [value.astimezone(zone) for value in sorted(candidates)]
