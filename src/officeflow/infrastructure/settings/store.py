from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class AppSettings:
    theme: str = "light"
    timezone: str = "Asia/Seoul"
    window_width: int = 1280
    window_height: int = 800
    window_x: int | None = None
    window_y: int | None = None
    compact_list: bool = False
    missed_reminder_grace_minutes: int = 120


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
