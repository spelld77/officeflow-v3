from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config

from officeflow.infrastructure.database.session import sqlite_url


def migration_config(database_file: Path) -> Config:
    script_location = Path(__file__).with_name("migrations")
    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", sqlite_url(database_file))
    return config


def needs_attachment_search_upgrade(database_file: Path) -> bool:
    if not database_file.is_file():
        return False
    with closing(sqlite3.connect(database_file)) as source:
        return bool(
            source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'"
            ).fetchone()
            and not source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachment_search'"
            ).fetchone()
        )


def upgrade_database(database_file: Path, *, progress: Callable[[str], None] | None = None) -> None:
    database_file.parent.mkdir(parents=True, exist_ok=True)
    if database_file.is_file():
        with closing(sqlite3.connect(database_file)) as source:
            needs_index = (
                source.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'"
                ).fetchone()
                and not source.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachment_search'"
                ).fetchone()
            )
            if needs_index:
                required = database_file.stat().st_size * 2 + 16 * 1024**2
                if shutil.disk_usage(database_file.parent).free < required:
                    raise OSError(
                        "첨부 검색 준비에 필요한 디스크 공간이 부족합니다. 여유 공간을 확보한 뒤 다시 실행해 주세요."
                    )
                if progress is not None:
                    progress("기존 데이터베이스의 보호 사본을 만들고 있습니다…")
                folder = database_file.parent / "schema-backups"
                folder.mkdir(exist_ok=True)
                backup = folder / f"pre-attachment-search-{datetime.now(UTC):%Y%m%dT%H%M%S%f}.db"
                try:
                    with closing(sqlite3.connect(backup)) as target:
                        source.backup(target)
                except Exception:
                    backup.unlink(missing_ok=True)
                    raise
    if progress is not None:
        progress("첨부파일명 검색 색인을 준비하고 있습니다…")
    command.upgrade(migration_config(database_file), "head")
