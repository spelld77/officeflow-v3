from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path

    @classmethod
    def discover(cls) -> AppPaths:
        override = os.environ.get("OFFICEFLOW_DATA_DIR")
        root = (
            Path(override).expanduser()
            if override
            else user_data_path("OfficeFlow", appauthor=False)
        )
        return cls(root=root.resolve())

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def database_file(self) -> Path:
        return self.data_dir / "officeflow.db"

    @property
    def attachment_dir(self) -> Path:
        return self.root / "attachments"

    @property
    def backup_dir(self) -> Path:
        return self.root / "backups"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    def ensure_directories(self) -> None:
        for path in (self.root, self.data_dir, self.attachment_dir, self.backup_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)
