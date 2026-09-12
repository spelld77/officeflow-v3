from __future__ import annotations

from pathlib import Path

from officeflow.infrastructure.settings.store import AppSettings, JsonSettingsStore


def test_settings_round_trip(tmp_path: Path) -> None:
    store = JsonSettingsStore(tmp_path / "settings.json")
    expected = AppSettings(theme="dark", compact_list=True)

    store.save(expected)

    assert store.load() == expected


def test_invalid_settings_fall_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("not json", encoding="utf-8")

    assert JsonSettingsStore(path).load() == AppSettings()
