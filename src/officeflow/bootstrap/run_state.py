from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class RunStateTracker:
    """Leave a small marker while OfficeFlow is running."""

    def __init__(self, marker_file: Path) -> None:
        self._marker_file = marker_file

    def begin(self) -> bool:
        previous_run_was_unclean = self._marker_file.exists()
        self._marker_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._marker_file.with_suffix(f"{self._marker_file.suffix}.tmp")
        payload = {
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(),
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self._marker_file)
        return previous_run_was_unclean

    def mark_clean(self) -> None:
        try:
            self._marker_file.unlink(missing_ok=True)
        except OSError:
            logger.exception("정상 종료 표식 파일을 정리하지 못했습니다.")

