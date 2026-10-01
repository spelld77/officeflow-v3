from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from officeflow.domain.hourly_notification import (
    HourBucket,
    HourlySettings,
    exclusion_reason,
    hour_bucket,
    local_time,
    regular_at,
    regular_candidates,
)


@dataclass(frozen=True, slots=True)
class HourlyState:
    enabled: bool
    bucket: HourBucket
    reason: str | None
    current_status: str
    next_regular_at: datetime | None
    snoozed_until: datetime | None
    open_key: str | None
    can_snooze: bool
    can_resume_morning: bool
    holiday_today: bool
    weekend: bool
    message: str
    early_morning_hint: bool
    error: str | None


class HourlyNotificationService:
    """Bounded, in-memory notification state. No attendance/record/delivery database."""

    def __init__(
        self,
        settings: HourlySettings,
        save_settings: Callable[[HourlySettings], None] | None = None,
    ) -> None:
        self.settings = settings
        self._save_settings = save_settings
        self.enabled = True
        self.active_since: datetime | None = None
        self._started = False
        self._stopped = False
        self._history: OrderedDict[str, str] = OrderedDict()
        self.open_key: str | None = None
        self.snoozed_until: datetime | None = None
        self._snooze_key: str | None = None
        self.request_key: str | None = None
        self.request_sound = False
        self._last_now: datetime | None = None
        self._last_monotonic: float | None = None
        self.error: str | None = None
        self._failed_key: str | None = None

    @property
    def history_size(self) -> int:
        return len(self._history)

    def _remember(self, key: str, status: str) -> None:
        self._history[key] = status
        self._history.move_to_end(key)
        while len(self._history) > 48:
            self._history.popitem(last=False)

    def _synchronize_dates(self, now: datetime) -> None:
        local = local_time(now, self.settings)
        today = local.date().isoformat()
        holiday = self.settings.holiday_date
        morning = self.settings.morning_continuation_date
        if holiday != today:
            holiday = None
        if morning != today or local.hour >= 9:
            morning = None
        threshold = local.replace(hour=8, minute=0, second=0, microsecond=0)
        if (
            self.enabled
            and local.hour < 9
            and self.active_since is not None
            and self.active_since.astimezone(UTC) < threshold.astimezone(UTC)
        ):
            morning = today
        updated = replace(self.settings, holiday_date=holiday, morning_continuation_date=morning)
        if updated == self.settings:
            return
        # Auto-continuation survives a save error in RAM; don't retry each timer tick.
        self.settings = updated
        try:
            if self._save_settings:
                self._save_settings(updated)
        except OSError:
            self.error = (
                "아침 연속/날짜 설정을 저장하지 못했습니다. 재실행 시 보존되지 않을 수 있습니다."
            )

    def _allowed(self, now: datetime) -> bool:
        return self.enabled and exclusion_reason(now, self.settings) is None

    def _request(self, key: str, *, sound: bool = True) -> None:
        if key != self._failed_key:
            self.request_key = key
            self.request_sound = sound

    def _clear_open(self) -> None:
        self.open_key = None
        self.request_key = None
        self.snoozed_until = None
        self._snooze_key = None

    def start(self, now: datetime, monotonic_now: float = 0.0) -> HourlyState:
        if self._started or self._stopped:
            return self.state(now)
        self._started = True
        self.active_since = now
        self._synchronize_dates(now)
        self._last_now = now.astimezone(UTC)
        self._last_monotonic = monotonic_now
        if self._allowed(now):
            self._request(hour_bucket(now, self.settings).key)
        return self.state(now)

    def tick(self, now: datetime, monotonic_now: float) -> HourlyState:
        if not self._started or self._stopped:
            return self.state(now)
        self._synchronize_dates(now)
        bucket = hour_bucket(now, self.settings)
        current = now.astimezone(UTC)
        delayed = self._last_now is not None and (
            abs((current - self._last_now).total_seconds()) >= 90
            or (self._last_monotonic is not None and monotonic_now - self._last_monotonic >= 90)
        )
        self._last_now, self._last_monotonic = current, monotonic_now
        if not self._allowed(now):
            self._clear_open()
            return self.state(now)
        was_open = self.open_key is not None
        if self.open_key is not None and self.open_key != bucket.key:
            self._clear_open()
        if self._snooze_key != bucket.key:
            self.snoozed_until = None
            self._snooze_key = None
        if self.request_key is not None and self.request_key != bucket.key:
            self.request_key = None
        status = self._history.get(bucket.key)
        if status in ("closed", "skipped"):
            self._clear_open()
        elif self.snoozed_until is not None:
            if current >= self.snoozed_until.astimezone(UTC):
                self.snoozed_until = None
                self._snooze_key = None
                self._request(bucket.key)
        elif self.open_key != bucket.key:
            if was_open and status is None:
                self._request(bucket.key, sound=False)
            elif (
                status is None
                and (delayed or current >= regular_at(bucket, self.settings).astimezone(UTC))
                and (
                    delayed
                    or exclusion_reason(regular_at(bucket, self.settings), self.settings) is None
                )
            ):
                # Nominal time inside an exclusion is skipped, not moved to its end.
                self._request(bucket.key)
        return self.state(now)

    def display_succeeded(self, key: str) -> None:
        if self.request_key != key or self._stopped:
            return
        self.open_key = key
        self.request_key = None
        self._remember(key, "seen")

    def display_failed(self, key: str) -> None:
        self.request_key = None
        self._failed_key = key
        self.error = "매시 알림창 표시에 실패했습니다. 다음 시간대에 다시 시도합니다."

    def _current_key(self, key: str, now: datetime) -> bool:
        return not self._stopped and key == hour_bucket(now, self.settings).key

    def dismiss(self, key: str, now: datetime) -> None:
        if self._current_key(key, now) and self.open_key == key:
            self._remember(key, "closed")
            self._clear_open()

    def skip_current(self, now: datetime) -> None:
        key = hour_bucket(now, self.settings).key
        if self._history.get(key) == "closed" or self._stopped:
            return
        self._remember(key, "skipped")
        self._clear_open()

    def cancel_skip(self, now: datetime) -> None:
        key = hour_bucket(now, self.settings).key
        if self._history.get(key) != "skipped":
            return
        del self._history[key]
        self._failed_key = None
        if self._allowed(now) and now.astimezone(UTC) >= regular_at(
            hour_bucket(now, self.settings), self.settings
        ).astimezone(UTC):
            self._request(key)

    def snooze(self, key: str, now: datetime) -> bool:
        if (
            not self._current_key(key, now)
            or self.open_key != key
            or not self.state(now).can_snooze
        ):
            return False
        self.snoozed_until = (now.astimezone(UTC) + timedelta(minutes=5)).astimezone(
            ZoneInfo(self.settings.timezone)
        )
        self._snooze_key = key
        self.open_key = None
        self.request_key = None
        return True

    def set_session_enabled(self, enabled: bool, now: datetime) -> None:
        if enabled == self.enabled or self._stopped:
            return
        self.enabled = enabled
        self._clear_open()
        if enabled:
            self.active_since = now
            self._synchronize_dates(now)
            key = hour_bucket(now, self.settings).key
            if self._allowed(now) and self._history.get(key) not in ("closed", "skipped"):
                self._failed_key = None
                self._request(key)

    def update_settings(self, settings: HourlySettings, now: datetime) -> None:
        if self._stopped:
            return
        if self._save_settings:
            self._save_settings(settings)
        timezone_changed = settings.timezone != self.settings.timezone
        self.settings = settings
        if timezone_changed:
            self._clear_open()
        self._synchronize_dates(now)
        if not self._allowed(now):
            self._clear_open()
        bucket = hour_bucket(now, self.settings)
        if self._history.get(bucket.key) is None and now.astimezone(UTC) >= regular_at(
            bucket, settings
        ).astimezone(UTC):
            self._remember(bucket.key, "seen")

    def set_today_holiday(self, enabled: bool, now: datetime) -> None:
        local = local_time(now, self.settings)
        if local.weekday() >= 5:
            return
        self._save_date_change(
            replace(self.settings, holiday_date=local.date().isoformat() if enabled else None), now
        )

    def resume_morning_notifications(self, now: datetime) -> None:
        if self.state(now).can_resume_morning:
            self._save_date_change(
                replace(
                    self.settings,
                    morning_continuation_date=local_time(now, self.settings).date().isoformat(),
                ),
                now,
            )

    def _save_date_change(self, updated: HourlySettings, now: datetime) -> None:
        if self._stopped:
            return
        if self._save_settings:
            self._save_settings(updated)
        self.settings = updated
        self._synchronize_dates(now)
        key = hour_bucket(now, self.settings).key
        if not self._allowed(now):
            self._clear_open()
        elif (
            self._history.get(key) not in ("closed", "skipped")
            and self.snoozed_until is None
            and self.open_key != key
        ):
            self._request(key)

    def state(self, now: datetime) -> HourlyState:
        bucket = hour_bucket(now, self.settings)
        local = local_time(now, self.settings)
        reason = exclusion_reason(now, self.settings)
        next_regular: datetime | None = None
        if self.enabled and not self._stopped:
            for candidate in regular_candidates(now, self.settings):
                if (
                    hour_bucket(candidate, self.settings).key in self._history
                    or hour_bucket(candidate, self.settings).key == self.request_key
                ):
                    continue
                if (
                    exclusion_reason(candidate, self.settings, active_since=self.active_since)
                    is None
                ):
                    next_regular = candidate
                    break
        snooze_at = now.astimezone(UTC) + timedelta(minutes=5)
        can_snooze = (
            self.enabled
            and reason is None
            and self.open_key == bucket.key
            and snooze_at < bucket.ends_at.astimezone(UTC)
            and exclusion_reason(snooze_at, self.settings) is None
        )
        holiday = self.settings.holiday_date == local.date().isoformat()
        return HourlyState(
            self.enabled,
            bucket,
            reason,
            self._history.get(bucket.key, "waiting"),
            next_regular,
            self.snoozed_until,
            self.open_key,
            can_snooze,
            local.weekday() < 5
            and not holiday
            and local.hour == 8
            and self.settings.morning_continuation_required
            and self.settings.morning_continuation_date != local.date().isoformat(),
            holiday,
            local.weekday() >= 5,
            self.settings.message.strip(),
            local.weekday() < 5
            and not holiday
            and local.hour < 8
            and self.settings.morning_continuation_required,
            self.error,
        )

    def shutdown(self) -> None:
        self._stopped = True
        self._clear_open()
        self._history.clear()
