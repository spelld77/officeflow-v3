from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

HELP_FILENAME = "OfficeFlow-사용자안내.html"


def user_help_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / HELP_FILENAME
    return Path(__file__).resolve().parents[3] / "packaging" / "windows" / HELP_FILENAME


def open_user_help() -> bool:
    path = user_help_path()
    return path.is_file() and QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
