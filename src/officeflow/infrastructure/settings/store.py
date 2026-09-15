from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class AppSettings:
    theme: str = "light"
    timezone: str = "Asia/Seoul"
    window_width: int = 1280
    window_height: int = 800
    window_x: int | None = None
    window_y: int | None = None
    compact_list: bool = False
    collapsed_today_groups: tuple[str, ...] = ("completed",)
    view_preferences: dict[str, dict[str, str | bool]] = field(default_factory=dict)
    missed_reminder_grace_minutes: int = 120
    minimize_to_tray: bool = True
    start_with_windows: bool = False
    global_quick_add_shortcut: str = "Ctrl+Alt+O"
    automatic_backup_enabled: bool = True
    automatic_backup_interval_hours: int = 24
    automatic_backup_keep: int = 10

    def __post_init__(self) -> None:
        if not 1 <= self.missed_reminder_grace_minutes <= 43_200:
            raise ValueError("놓친 알림 복구 범위는 1분에서 30일 사이여야 합니다.")
        if not self.global_quick_add_shortcut.strip():
            raise ValueError("전역 빠른 등록 단축키가 비어 있습니다.")
        if not 1 <= self.automatic_backup_interval_hours <= 24 * 30:
            raise ValueError("자동 백업 주기는 1시간에서 30일 사이여야 합니다.")
        if not 1 <= self.automatic_backup_keep <= 100:
            raise ValueError("자동 백업 보관 개수는 1개에서 100개 사이여야 합니다.")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("지원하지 않는 시간대입니다.") from error


class JsonSettingsStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> AppSettings:
        if not self._path.exists():
            return AppSettings()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return AppSettings()
            allowed = AppSettings.__dataclass_fields__.keys()
            values = {key: value for key, value in raw.items() if key in allowed}
            collapsed = values.get("collapsed_today_groups")
            if isinstance(collapsed, list):
                values["collapsed_today_groups"] = tuple(
                    value for value in collapsed if isinstance(value, str)
                )
            preferences = values.get("view_preferences")
            if preferences is not None and not isinstance(preferences, dict):
                values.pop("view_preferences")
            return AppSettings(**values)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = asdict(settings)
        handle, temporary_name = tempfile.mkstemp(
            prefix="settings-", suffix=".tmp", dir=self._path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(self._path)
        finally:
            temporary_path.unlink(missing_ok=True)
