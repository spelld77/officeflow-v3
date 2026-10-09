import subprocess
import sys
from uuid import uuid4

import pytest

from officeflow.application.tasks import TaskDraft
from tests.integration.test_attachment_search import search_environment


@pytest.mark.parametrize("boundary", ["history", "receipt", "committed", "returned"])
def test_task_history_real_process_exit_before_and_after_commit(tmp_path, boundary):
    identity = uuid4().hex
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft("before"))
        path = env.database
    code = """
import os, sys
from pathlib import Path
from sqlalchemy import event
from officeflow.application.tasks import TaskService, TaskDraft
from officeflow.infrastructure.database.models import TaskHistoryRecord, TaskMutationReceiptRecord
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
engine=create_database_engine(Path(sys.argv[1]))
tasks=TaskService(SqlAlchemyTaskRepository(SessionFactory(engine)))
stage=sys.argv[4]
if stage in {'history','receipt'}:
    event.listen(TaskHistoryRecord if stage=='history' else TaskMutationReceiptRecord,'before_insert',lambda *args:os._exit(74))
elif stage=='committed':
    tasks.history.subscribe(lambda task_id:os._exit(74))
tasks.update(int(sys.argv[2]),TaskDraft('after'),command_id=sys.argv[3])
os._exit(74)
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(path), str(task.id), identity, boundary],
        capture_output=True,
        timeout=30,
    )
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    with search_environment(tmp_path) as env:
        completed = boundary in {"committed", "returned"}
        assert env.tasks.get(task.id).title == ("after" if completed else "before")
        assert len(env.tasks.history.page(task.id).items) == (2 if completed else 1)
        result = env.tasks.update(task.id, TaskDraft("after"), command_id=identity)
        assert result.command_replayed is completed
        assert len(env.tasks.history.page(task.id).items) == 2
