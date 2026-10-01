from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from officeflow.application.hourly_notifications import HourlyNotificationService
from officeflow.domain.hourly_notification import (
    HourlySettings,
    exclusion_intervals,
    exclusion_reason,
    hour_bucket,
    regular_candidates,
)


def at(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Seoul"))


def show(service: HourlyNotificationService) -> str:
    assert service.request_key is not None
    key = service.request_key
    service.display_succeeded(key)
    return key


@pytest.mark.parametrize("day", range(1, 8))
@pytest.mark.parametrize(
    "hour,expected",
    [
        (0, None),
        (7, None),
        (8, "morning_without_continuation"),
        (9, "weekday_exclusion"),
        (17, "weekday_exclusion"),
        (18, None),
        (23, None),
    ],
)
def test_all_weekdays_and_hour_boundaries(day: int, hour: int, expected: str | None) -> None:
    now = at(f"2026-10-{day:02d}T{hour:02d}:00:00")
    assert exclusion_reason(now, HourlySettings()) == (None if now.weekday() >= 5 else expected)


@pytest.mark.parametrize(
    "changes",
    [
        {"minute": True},
        {"minute": -1},
        {"minute": 60},
        {"minute": "35"},
        {"message": " "},
        {"message": "x" * 201},
        {"sound": 1},
        {"weekday_exclusion_start": "9:00"},
        {"weekday_exclusion_end": "24:00"},
        {"weekday_exclusion_start": "18:00"},
        {"holiday_date": "2026-2-3"},
        {"extra_exclusions": (("12:00", "12:00"),)},
        {"extra_exclusions": (("12:00", "13:00"),) * 9},
    ],
)
def test_invalid_settings(changes: dict) -> None:
    with pytest.raises((ValueError, TypeError)):
        HourlySettings(**changes)


@pytest.mark.parametrize("minute", [0, 35, 59])
@pytest.mark.parametrize("time", ["19:10", "19:45"])
def test_start_immediate_once_and_no_same_hour_regular(minute: int, time: str) -> None:
    now = at(f"2026-10-01T{time}")
    service = HourlyNotificationService(HourlySettings(minute=minute))
    service.start(now)
    key = show(service)
    service.dismiss(key, now)
    service.start(now)
    service.tick(now + timedelta(seconds=5), 5)
    assert service.request_key is None
    assert service.state(now).next_regular_at == at(f"2026-10-01T20:{minute:02d}")


def test_business_start_enters_evening_at_regular_minute_not_1800() -> None:
    service = HourlyNotificationService(HourlySettings())
    now = at("2026-10-01T17:59:00")
    service.start(now)
    for second in range(30, 36 * 60 + 1, 30):
        service.tick(now + timedelta(seconds=second), float(second))
        if second < 36 * 60:
            assert service.request_key is None
    assert service.request_key == hour_bucket(at("2026-10-01T18:35"), service.settings).key
    show(service)
    service.tick(at("2026-10-01T18:35:05"), 2165)
    assert service.request_key is None


def test_morning_first_start_is_excluded_but_manual_resume_works() -> None:
    now = at("2026-10-01T08:30")
    saved = []
    service = HourlyNotificationService(HourlySettings(), saved.append)
    state = service.start(now)
    assert service.request_key is None
    assert state.reason == "morning_without_continuation"
    assert state.next_regular_at == at("2026-10-01T18:35")
    service.tick(at("2026-10-01T08:40"), 600)
    assert service.request_key is None
    service.resume_morning_notifications(now)
    show(service)
    assert len(saved) == 1
    assert service.settings.morning_continuation_date == "2026-10-01"


@pytest.mark.parametrize(
    "start", ["2026-10-01T05:00", "2026-10-01T07:50", "2026-10-01T07:59", "2026-09-30T19:00"]
)
def test_early_and_overnight_on_automatically_continue_into_08(start: str) -> None:
    saved = []
    service = HourlyNotificationService(HourlySettings(), saved.append)
    initial = at(start)
    service.start(initial)
    service.dismiss(show(service), initial)
    service.tick(at("2026-10-01T08:30"), 100_000)
    assert service.settings.morning_continuation_date == "2026-10-01"
    show(service)
    service.tick(at("2026-10-01T09:00"), 102_000)
    assert service.open_key is None
    assert service.request_key is None
    assert service.settings.morning_continuation_date is None
    count = len(saved)
    for _ in range(5):
        service.tick(at("2026-10-01T09:00:05"), 102_005)
    assert len(saved) == count


def test_morning_continuation_survives_restart_without_preserving_closed_or_off() -> None:
    service = HourlyNotificationService(HourlySettings())
    service.start(at("2026-10-01T05:00"))
    service.set_session_enabled(False, at("2026-10-01T07:00"))
    restarted = HourlyNotificationService(service.settings)
    assert restarted.start(at("2026-10-01T08:30")).reason is None
    show(restarted)
    assert restarted.enabled


def test_overnight_off_to_on_at_0830_does_not_use_old_process_start() -> None:
    service = HourlyNotificationService(HourlySettings())
    initial = at("2026-09-30T19:00")
    service.start(initial)
    service.set_session_enabled(False, initial)
    now = at("2026-10-01T08:30")
    service.tick(now, 50_000)
    service.set_session_enabled(True, now)
    assert service.request_key is None
    assert service.settings.morning_continuation_date is None
    assert service.state(now).reason == "morning_without_continuation"


def test_automatic_save_failure_keeps_ram_continuation_and_no_repeated_writes() -> None:
    calls = []

    def fail(value: HourlySettings) -> None:
        calls.append(value)
        raise OSError("disk full")

    service = HourlyNotificationService(HourlySettings(), fail)
    service.start(at("2026-10-01T05:00"))
    show(service)
    for second in range(5, 30, 5):
        service.tick(at("2026-10-01T05:00") + timedelta(seconds=second), float(second))
    assert len(calls) == 1
    assert service.settings.morning_continuation_date == "2026-10-01"
    assert service.error is not None


@pytest.mark.parametrize("action", ["holiday", "morning", "settings"])
def test_manual_save_failure_rolls_back(action: str) -> None:
    def fail(_value: HourlySettings) -> None:
        raise OSError("denied")

    now = at("2026-10-01T08:30")
    service = HourlyNotificationService(HourlySettings(), fail)
    service.start(now)
    previous = service.settings
    with pytest.raises(OSError):
        if action == "holiday":
            service.set_today_holiday(True, now)
        elif action == "morning":
            service.resume_morning_notifications(now)
        else:
            service.update_settings(replace(previous, minute=40), now)
    assert service.settings == previous
    assert service.request_key is None


def test_holiday_immediate_restart_expiry_and_extra_exclusion_priority() -> None:
    now = at("2026-10-01T12:00")
    service = HourlyNotificationService(HourlySettings())
    service.start(now)
    service.set_today_holiday(True, now)
    show(service)
    restarted = HourlyNotificationService(service.settings)
    restarted.start(now)
    show(restarted)
    restarted.tick(at("2026-10-02T12:00"), 86_400)
    assert restarted.settings.holiday_date is None
    assert restarted.open_key is None
    excluded = HourlyNotificationService(
        HourlySettings(holiday_date="2026-10-01", extra_exclusions=(("11:00", "13:00"),))
    )
    assert excluded.start(now).reason == "extra_exclusion"
    assert excluded.request_key is None


def test_skip_cancel_and_closed_hour_survive_setting_changes_and_on_toggle() -> None:
    now = at("2026-10-01T19:10")
    service = HourlyNotificationService(HourlySettings())
    service.start(now)
    show(service)
    service.skip_current(now)
    service.update_settings(replace(service.settings, minute=15), now)
    service.set_session_enabled(False, now)
    service.set_session_enabled(True, now)
    assert service.request_key is None
    service.cancel_skip(now)
    assert service.request_key is None
    service.tick(at("2026-10-01T19:15"), 300)
    service.dismiss(show(service), at("2026-10-01T19:15"))
    service.update_settings(replace(service.settings, minute=45), now)
    service.set_session_enabled(False, now)
    service.set_session_enabled(True, now)
    assert service.request_key is None
    assert service.state(now).current_status == "closed"


def test_minute_change_does_not_recover_a_new_past_minute() -> None:
    now = at("2026-10-01T18:00")
    service = HourlyNotificationService(HourlySettings())
    service.start(at("2026-10-01T17:59"))
    service.tick(now, 60)
    service.update_settings(replace(service.settings, minute=0), now)
    service.tick(now + timedelta(seconds=5), 65)
    assert service.request_key is None
    assert service.state(now).next_regular_at == at("2026-10-01T19:00")


@pytest.mark.parametrize("minute,allowed", [(54, True), (55, False), (59, False)])
def test_snooze_boundaries(minute: int, allowed: bool) -> None:
    now = at(f"2026-10-01T19:{minute}")
    service = HourlyNotificationService(HourlySettings())
    service.start(now)
    key = show(service)
    assert service.snooze(key, now) is allowed
    assert service.state(now).next_regular_at == at("2026-10-01T20:35")
    if allowed:
        assert service.snoozed_until == now + timedelta(minutes=5)
        service.tick(now + timedelta(minutes=5), 300)
        assert show(service) == key


def test_snooze_survives_minute_edit_but_not_hour_boundary_off_or_exclusion() -> None:
    now = at("2026-10-01T19:40")
    service = HourlyNotificationService(HourlySettings())
    service.start(now)
    key = show(service)
    service.snooze(key, now)
    service.update_settings(replace(service.settings, minute=50), now)
    assert service.snoozed_until == at("2026-10-01T19:45")
    service.update_settings(replace(service.settings, extra_exclusions=(("19:00", "20:00"),)), now)
    assert service.snoozed_until is None
    assert service.request_key is None


def test_open_window_updates_hour_silently_and_expires_at_business_time() -> None:
    service = HourlyNotificationService(HourlySettings())
    service.start(at("2026-10-01T07:59:55"))
    old = show(service)
    service.tick(at("2026-10-01T08:00"), 5)
    assert not service.request_sound
    current = show(service)
    service.dismiss(old, at("2026-10-01T08:00"))
    assert service.open_key == current
    service.tick(at("2026-10-01T09:00"), 3605)
    assert service.open_key is None
    assert service.request_key is None


def test_resume_is_current_hour_only_and_clock_backwards_does_not_repeat_closed() -> None:
    service = HourlyNotificationService(HourlySettings())
    initial = at("2026-10-01T18:05")
    service.start(initial)
    service.dismiss(show(service), initial)
    resumed = at("2026-10-01T21:10")
    service.tick(resumed, 10_000)
    assert show(service) == hour_bucket(resumed, service.settings).key
    assert service.history_size == 2
    service.tick(initial, 10_005)
    assert service.request_key is None
    assert service.open_key is None


def test_failed_display_is_not_success_and_not_retried_each_tick() -> None:
    service = HourlyNotificationService(HourlySettings())
    now = at("2026-10-01T19:40")
    service.start(now)
    assert service.request_key is not None
    service.display_failed(service.request_key)
    for second in range(5, 60, 5):
        service.tick(now + timedelta(seconds=second), float(second))
        assert service.request_key is None
    assert service.history_size == 0
    service.tick(at("2026-10-01T20:40"), 3600)
    show(service)


def test_many_days_bounded_state_and_idempotent_shutdown() -> None:
    service = HourlyNotificationService(HourlySettings())
    now = at("2026-10-03T00:00")
    service.start(now)
    for index in range(240):
        current = now + timedelta(hours=index)
        service.tick(current, index * 3600)
        if service.request_key:
            service.dismiss(show(service), current)
        assert service.history_size <= 48
    service.shutdown()
    service.shutdown()
    service.tick(now, 999_999)
    service.start(now)
    assert service.request_key is None
    assert service.open_key is None


def test_wrapped_overlapping_exclusions_normalized_and_no_candidate() -> None:
    assert exclusion_intervals((("23:00", "06:00"), ("05:00", "07:00"))) == ((0, 420), (1380, 1440))
    service = HourlyNotificationService(
        HourlySettings(extra_exclusions=(("00:00", "12:00"), ("12:00", "00:00")))
    )
    state = service.start(at("2026-10-03T10:00"))
    assert state.next_regular_at is None
    assert service.request_key is None


def test_dst_folds_distinct_and_gap_regular_time_does_not_exist() -> None:
    settings = HourlySettings(timezone="America/New_York")
    zone = ZoneInfo(settings.timezone)
    first = datetime(2026, 11, 1, 1, 10, tzinfo=zone, fold=0)
    second = datetime(2026, 11, 1, 1, 10, tzinfo=zone, fold=1)
    assert hour_bucket(first, settings).key != hour_bucket(second, settings).key
    candidates = regular_candidates(first, settings)
    repeated = [c for c in candidates if c.date() == first.date() and c.hour == 1]
    assert len(repeated) == 2
    assert repeated[0].astimezone(UTC) != repeated[1].astimezone(UTC)
    gap = regular_candidates(datetime(2026, 3, 8, 0, tzinfo=zone), settings)
    assert not any(c.day == 8 and c.hour == 2 for c in gap)


def test_regular_inside_exclusion_does_not_move_to_exclusion_end() -> None:
    service = HourlyNotificationService(HourlySettings(extra_exclusions=(("18:34", "18:40"),)))
    initial = at("2026-10-01T17:59")
    service.start(initial)
    for seconds in range(30, 42 * 60, 30):
        service.tick(initial + timedelta(seconds=seconds), float(seconds))
        assert service.request_key is None
    assert service.state(at("2026-10-01T18:40")).next_regular_at == at("2026-10-01T19:35")


@pytest.mark.parametrize("initial,later", [
    ("2026-10-31T23:59:55", "2026-11-01T00:00"),
    ("2026-12-31T23:59:55", "2027-01-01T00:00"),
    ("2026-10-02T23:59:55", "2026-10-03T00:00"),
    ("2026-10-04T23:59:55", "2026-10-05T00:00"),
])
def test_day_month_year_and_weekend_boundary_updates(initial: str, later: str) -> None:
    service = HourlyNotificationService(HourlySettings())
    service.start(at(initial))
    old = show(service)
    service.tick(at(later), 5)
    assert show(service) != old
    assert not service.request_sound


def test_generic_morning_condition_off_and_stale_saved_dates_expire() -> None:
    service = HourlyNotificationService(HourlySettings(morning_continuation_required=False,
        holiday_date="2026-09-30", morning_continuation_date="2026-09-30"))
    state = service.start(at("2026-10-01T08:30"))
    assert state.reason is None
    show(service)
    assert service.settings.holiday_date is None
    assert service.settings.morning_continuation_date is None


def test_snooze_delayed_beyond_hour_is_discarded_not_replayed() -> None:
    service = HourlyNotificationService(HourlySettings())
    now = at("2026-10-01T19:40")
    service.start(now)
    old = show(service)
    service.snooze(old, now)
    service.tick(at("2026-10-01T21:10"), 5400)
    assert service.snoozed_until is None
    assert show(service) != old
    assert service.history_size == 2
