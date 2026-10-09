from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from officeflow.application.task_events import TaskChangeEvents


def sqlite_url(database_file: Path) -> str:
    return f"sqlite:///{database_file.resolve().as_posix()}"


def create_database_engine(database_file: Path, *, echo: bool = False) -> Engine:
    database_file.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(sqlite_url(database_file), echo=echo, hide_parameters=True)

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


class SessionFactory:
    def __init__(
        self, engine: Engine | Connection, *, events: TaskChangeEvents | None = None
    ) -> None:
        self._bind = engine
        self._factory = sessionmaker(bind=engine, expire_on_commit=False)
        self.task_events = events or TaskChangeEvents()

    @contextmanager
    def write_unit_of_work(self) -> Iterator[SessionFactory]:
        if isinstance(self._bind, Connection):
            yield self
            return
        with self._bind.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield SessionFactory(connection, events=self.task_events)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @contextmanager
    def read_snapshot(self) -> Iterator[SessionFactory]:
        if isinstance(self._bind, Connection):
            yield self
            return
        with self._bind.connect() as connection:
            # sqlite3's legacy transaction mode does not BEGIN for SELECT.
            connection.exec_driver_sql("BEGIN")
            try:
                yield SessionFactory(connection, events=self.task_events)
            finally:
                connection.rollback()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        session = self._factory()
        try:
            with session.begin():
                yield session
        finally:
            session.close()
