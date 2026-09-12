from __future__ import annotations

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


def upgrade_database(database_file: Path) -> None:
    database_file.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(migration_config(database_file), "head")
