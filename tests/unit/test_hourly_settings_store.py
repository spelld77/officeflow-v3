import json
from dataclasses import asdict
from pathlib import Path

from officeflow.infrastructure.settings.store import AppSettings, JsonSettingsStore


def test_new_field_repair_does_not_reset_existing_settings(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "automatic_backup_keep": 22,
                "window_width": 900,
                "start_with_windows": True,
                "hourly_notification_minute": True,
                "hourly_notification_message": "custom",
                "hourly_notification_sound": "yes",
                "hourly_notification_holiday_date": "invalid",
                "hourly_notification_extra_exclusions": ["bad"],
            }
        ),
        encoding="utf-8",
    )
    result = JsonSettingsStore(path).load()
    assert result.automatic_backup_keep == 22
    assert result.window_width == 900
    assert result.start_with_windows
    assert result.hourly_notification_minute == 35
    assert result.hourly_notification_message == "custom"
    assert not result.hourly_notification_sound
    assert result.hourly_notification_holiday_date is None
    assert result.hourly_notification_extra_exclusions == ()


def test_hourly_settings_roundtrip_dates_ranges_and_no_runtime_keys(tmp_path: Path) -> None:
    store = JsonSettingsStore(tmp_path / "settings.json")
    settings = AppSettings(
        hourly_notification_minute=59,
        hourly_notification_weekday_exclusion_start="18:00",
        hourly_notification_weekday_exclusion_end="08:00",
        hourly_notification_extra_exclusions=(("23:00", "01:00"),),
        hourly_notification_holiday_date="2026-10-01",
        hourly_notification_morning_continuation_date="2026-10-01",
    )
    store.save(settings)
    assert store.load() == settings
    payload = asdict(store.load())
    assert not {"enabled", "skipped_hour", "last_recorded_at", "last_delivery"}.intersection(
        payload
    )


def test_old_file_retains_backup_and_new_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"automatic_backup_interval_hours": 48}', encoding="utf-8")
    settings = JsonSettingsStore(path).load()
    assert settings.automatic_backup_interval_hours == 48
    assert settings.hourly_notification_minute == 35
