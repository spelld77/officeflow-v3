from __future__ import annotations

from pathlib import Path

from officeflow.infrastructure.settings.store import AppSettings, JsonSettingsStore


def test_settings_round_trip(tmp_path: Path) -> None:
    store = JsonSettingsStore(tmp_path / "settings.json")
    expected = AppSettings(
        theme="dark",
        compact_list=True,
        collapsed_today_groups=("overdue", "completed"),
        view_preferences={"all": {"status": "active", "pinned_only": True}},
    )

    store.save(expected)

    assert store.load() == expected


def test_invalid_settings_fall_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("not json", encoding="utf-8")

    assert JsonSettingsStore(path).load() == AppSettings()


def test_export_directory_round_trip_and_invalid_field_preserves_other_settings(tmp_path):
    import json

    store = JsonSettingsStore(tmp_path / "settings.json")
    expected = AppSettings(compact_list=True, attachment_export_directory=str(tmp_path))
    store.save(expected)
    assert store.load() == expected
    raw = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    raw["attachment_export_directory"] = ["invalid"]
    (tmp_path / "settings.json").write_text(json.dumps(raw), encoding="utf-8")
    loaded = store.load()
    assert loaded.compact_list
    assert loaded.attachment_export_directory is None


def test_invalid_reminder_grace_setting_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"missed_reminder_grace_minutes": 0}', encoding="utf-8")

    assert JsonSettingsStore(path).load() == AppSettings()


def test_record_window_size_round_trip_and_field_local_repair(tmp_path: Path) -> None:
    import json

    store = JsonSettingsStore(tmp_path / "settings.json")
    expected = AppSettings(
        record_window_width=940, record_window_height=720, automatic_backup_keep=7
    )
    store.save(expected)
    assert store.load() == expected
    raw = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    for value in (True, "940", -1, 0, 9000, [940]):
        raw["record_window_width"] = value
        (tmp_path / "settings.json").write_text(json.dumps(raw), encoding="utf-8")
        result = store.load()
        assert result.record_window_width is None
        assert result.record_window_height == 720
        assert result.automatic_backup_keep == 7
