from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "officeflow.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not any(
        isinstance(existing, RotatingFileHandler)
        and Path(existing.baseFilename) == (log_dir / "officeflow.log").resolve()
        for existing in root_logger.handlers
    ):
        root_logger.addHandler(handler)
