"""File-picker hints, not source-file ownership or startup directory scanning."""

import ctypes
import os
from pathlib import Path

from PySide6.QtCore import QStandardPaths

from officeflow.infrastructure.settings.store import valid_import_directory


def _remote_directory(path: Path) -> bool:
    if str(path).startswith(("\\\\", "//")):
        return True
    if os.name == "nt":
        # DRIVE_REMOTE also identifies mapped network drives; never stat them on the GUI thread.
        try:
            return int(ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor))) in (0, 1, 4)
        except (OSError, AttributeError):
            return True
    return False


def import_start_directory(directory: str | None) -> str:
    default = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    if not valid_import_directory(directory):
        return default
    path = Path(str(directory))
    if _remote_directory(path):
        return str(path)  # The native picker handles unavailable shares, not a preflight scan.
    try:
        return str(path) if path.is_dir() else default
    except OSError:
        return default
