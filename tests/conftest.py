from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from officeflow.application.tasks import TaskService
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository
from officeflow.presentation.background import ShutdownOwner, ShutdownScope


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item):
    result = yield
    from PySide6.QtCore import QCoreApplication, QEvent

    application = QCoreApplication.instance()
    if application is not None:
        # processEvents alone does not flush DeferredDelete. Do not carry old
        # windows/style objects into the next QApplication palette/style setup.
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()
    return result


@pytest.fixture
def qtbot(qtbot):
    """pytest-qt must join async owners before its unconditional deleteLater.

    Tests still exercise non-blocking close explicitly. Only fixture teardown
    uses the synchronous fallback, before dependent DB fixtures are disposed.
    """
    from shiboken6 import isValid

    original_add = qtbot.addWidget

    def add_widget(widget, *, before_close_func=None):
        def before_close(current):
            for scope in current.findChildren(ShutdownScope):
                if not isValid(scope) or scope.is_stopped:
                    continue
                if isinstance(scope.owner, ShutdownOwner):
                    scope.owner.shutdown()
                else:
                    scope.wait_for_stopped()
            if before_close_func is not None:
                before_close_func(current)

        original_add(widget, before_close_func=before_close)

    qtbot.addWidget = add_widget
    qtbot.add_widget = add_widget
    return qtbot


@pytest.fixture
def task_service(tmp_path: Path) -> Iterator[TaskService]:
    database_file = tmp_path / "officeflow.db"
    upgrade_database(database_file)
    engine = create_database_engine(database_file)
    service = TaskService(SqlAlchemyTaskRepository(SessionFactory(engine)))
    yield service
    engine.dispose()
