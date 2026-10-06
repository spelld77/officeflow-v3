from collections.abc import Iterator
from contextlib import contextmanager

from officeflow.application.reminders import ReminderService
from officeflow.application.task_saving import TaskSaveContext, TaskSaveService
from officeflow.application.tasks import TaskService
from officeflow.infrastructure.database.reminder_repository import SqlAlchemyReminderRepository
from officeflow.infrastructure.database.session import SessionFactory
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository


def task_save_service(sessions: SessionFactory, timezone: str) -> TaskSaveService:
    @contextmanager
    def unit_of_work() -> Iterator[TaskSaveContext]:
        with sessions.write_unit_of_work() as scoped:
            tasks = TaskService(SqlAlchemyTaskRepository(scoped), timezone=timezone)
            repository = SqlAlchemyReminderRepository(scoped)
            yield TaskSaveContext(tasks, ReminderService(repository, tasks), repository)

    return TaskSaveService(unit_of_work)
