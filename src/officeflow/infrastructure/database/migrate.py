from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

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


def needs_database_upgrade(database_file: Path) -> bool:
    """Detect all supported upgrades, not just the filename-search migration."""
    if not database_file.is_file():
        return False
    with closing(sqlite3.connect(database_file)) as source:
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            return False
        if not {"tasks", "attachments", "alembic_version"} <= tables:
            raise ValueError("OfficeFlow 데이터베이스 구조를 확인할 수 없습니다. 원본은 변경하지 않았습니다.")
        revisions = {row[0] for row in source.execute("SELECT version_num FROM alembic_version")}
    scripts = ScriptDirectory.from_config(migration_config(database_file))
    known = {revision.revision for revision in scripts.walk_revisions()}
    if len(revisions) != 1 or not revisions <= known:
        raise ValueError("지원하지 않는 DB 버전입니다. 이 DB를 지원하는 최신 OfficeFlow로 실행하세요.")
    return revisions != set(scripts.get_heads())


def upgrade_database(database_file: Path, *, progress: Callable[[str], None] | None = None) -> None:
    database_file.parent.mkdir(parents=True, exist_ok=True)
    if needs_database_upgrade(database_file):
        with closing(sqlite3.connect(database_file)) as source:
            needs_index = (
                source.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'"
                ).fetchone()
                and not source.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachment_search'"
                ).fetchone()
            )
            size = source.execute("PRAGMA page_count").fetchone()[0] * source.execute("PRAGMA page_size").fetchone()[0]
            required = size * 2 + 16 * 1024**2
            if shutil.disk_usage(database_file.parent).free < required:
                raise OSError(
                    "DB 업그레이드 보호 사본에 필요한 디스크 공간이 부족합니다. 여유 공간을 확보한 뒤 다시 실행해 주세요."
                )
            if progress is not None:
                progress("기존 데이터베이스의 보호 사본을 만들고 있습니다…")
            folder = database_file.parent / "schema-backups"
            folder.mkdir(exist_ok=True)
            has_order = any(row[1] == "sort_order" for row in source.execute("PRAGMA table_info(attachments)"))
            reason = "attachment-search" if needs_index else "attachment-order" if not has_order else "attachment-observation"
            backup = folder / f"pre-{reason}-{datetime.now(UTC):%Y%m%dT%H%M%S%f}.db"
            try:
                with closing(sqlite3.connect(backup)) as target:
                    source.backup(target)
                    if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("DB 보호 사본의 무결성을 확인하지 못했습니다.")
            except Exception:
                backup.unlink(missing_ok=True)
                raise
    if progress is not None:
        progress("데이터베이스와 첨부파일 정보를 준비하고 있습니다…")
    command.upgrade(migration_config(database_file), "head")
