from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr  # type: ignore[import-untyped]


class RecurrenceValidationError(ValueError):
    """Raised when a recurrence rule cannot be safely interpreted."""


class RecurrenceFrequency(StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


@dataclass(frozen=True, slots=True)
class RecurrenceSpec:
    frequency: RecurrenceFrequency
    interval: int = 1
    until: date | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.interval <= 99:
            raise RecurrenceValidationError("반복 간격은 1~99 사이여야 합니다.")

    def to_rrule(self, timezone: str) -> str:
        parts = [f"FREQ={self.frequency.value}", f"INTERVAL={self.interval}"]
        if self.until is not None:
            zone = ZoneInfo(timezone)
            exclusive_end = datetime.combine(self.until + timedelta(days=1), time.min, tzinfo=zone)
            until = (exclusive_end - timedelta(microseconds=1)).astimezone(UTC)
            parts.append(until.strftime("UNTIL=%Y%m%dT%H%M%SZ"))
        return ";".join(parts)


def parse_simple_recurrence(rule: str, timezone: str = "UTC") -> RecurrenceSpec | None:
    fields: dict[str, str] = {}
    for token in rule.strip().removeprefix("RRULE:").split(";"):
        if "=" not in token:
            return None
        key, value = token.split("=", 1)
        fields[key.upper()] = value.upper()
    if set(fields) - {"FREQ", "INTERVAL", "UNTIL"}:
        return None
    try:
        frequency = RecurrenceFrequency(fields["FREQ"])
        interval = int(fields.get("INTERVAL", "1"))
        until_value = fields.get("UNTIL")
        until = None
        if until_value:
            until = (
                datetime.strptime(until_value, "%Y%m%dT%H%M%SZ")
                .replace(tzinfo=UTC)
                .astimezone(ZoneInfo(timezone))
                .date()
            )
        return RecurrenceSpec(frequency=frequency, interval=interval, until=until)
    except (KeyError, ValueError):
        return None


def validate_recurrence_rule(rule: str, starts_at: datetime) -> str:
    normalized = rule.strip().removeprefix("RRULE:")
    if not normalized:
        raise RecurrenceValidationError("반복 규칙이 비어 있습니다.")
    if starts_at.tzinfo is None:
        raise RecurrenceValidationError("반복 일정의 시작 시각에 시간대가 필요합니다.")
    try:
        parsed: Any = rrulestr(normalized, dtstart=starts_at)
        parsed.after(starts_at - timedelta(microseconds=1), inc=True)
    except (TypeError, ValueError, OverflowError) as error:
        raise RecurrenceValidationError("지원하지 않거나 잘못된 반복 규칙입니다.") from error
    return normalized


def recurrence_until_utc(rule: str | None) -> datetime | None:
    if not rule:
        return None
    fields = _rule_fields(rule)
    value = fields.get("UNTIL")
    if not value:
        return None
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            parsed = datetime.strptime(value, pattern)
            if pattern == "%Y%m%d":
                parsed = datetime.combine(parsed.date() + timedelta(days=1), time.min) - timedelta(
                    microseconds=1
                )
            return parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def expand_recurrence(
    rule: str,
    *,
    template_start: datetime,
    template_end: datetime | None,
    timezone: str,
    range_start: datetime,
    range_end: datetime,
) -> tuple[tuple[datetime, datetime | None], ...]:
    if range_end <= range_start:
        raise ValueError("반복 조회 종료 시각은 시작 시각보다 늦어야 합니다.")
    normalized = validate_recurrence_rule(rule, template_start)
    zone = ZoneInfo(timezone)
    local_template = template_start.astimezone(zone)
    duration = template_end - template_start if template_end is not None else None
    search_start = range_start - (duration or timedelta(0))
    parsed: Any = rrulestr(normalized, dtstart=local_template)
    starts = parsed.between(search_start.astimezone(zone), range_end.astimezone(zone), inc=True)
    result: list[tuple[datetime, datetime | None]] = []
    for local_start in starts:
        occurrence_start = local_start.astimezone(UTC)
        occurrence_end = occurrence_start + duration if duration is not None else None
        overlaps = occurrence_start < range_end and (
            occurrence_end > range_start
            if occurrence_end is not None
            else occurrence_start >= range_start
        )
        if overlaps:
            result.append((occurrence_start, occurrence_end))
    return tuple(result)


def next_recurrence_start(
    rule: str,
    *,
    template_start: datetime,
    timezone: str,
    after: datetime,
    inclusive: bool = False,
) -> datetime | None:
    normalized = validate_recurrence_rule(rule, template_start)
    zone = ZoneInfo(timezone)
    parsed: Any = rrulestr(normalized, dtstart=template_start.astimezone(zone))
    result = parsed.after(after.astimezone(zone), inc=inclusive)
    return result.astimezone(UTC) if result is not None else None


def _rule_fields(rule: str) -> dict[str, str]:
    return {
        key.upper(): value.upper()
        for token in rule.strip().removeprefix("RRULE:").split(";")
        if "=" in token
        for key, value in (token.split("=", 1),)
    }
