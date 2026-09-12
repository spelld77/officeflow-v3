from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def sqlite_url(database_file: Path) -> str:
    return f"sqlite:///{database_file.resolve().as_posix()}"


def create_database_engine(database_file: Path, *, echo: bool = False) -> Engine:
    database_file.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(sqlite_url(database_file), echo=echo)

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


class SessionFactory:
    def __init__(self, engine: Engine) -> None:
        self._factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        session = self._factory()
        try:
            with session.begin():
                yield session
        finally:
            session.close()
