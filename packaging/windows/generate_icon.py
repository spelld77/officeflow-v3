from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from officeflow.presentation.app_icon import create_app_icon


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: generate_icon.py OUTPUT.ico")
    destination = Path(sys.argv[1]).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    pixmap = create_app_icon().pixmap(256, 256)
    if pixmap.isNull() or not pixmap.save(str(destination), "ICO"):
        raise RuntimeError("OfficeFlow 아이콘을 만들지 못했습니다.")
    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
