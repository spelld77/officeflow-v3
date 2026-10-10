from datetime import UTC, datetime

from sqlalchemy import select

from officeflow.application.folder_links import FolderLink
from officeflow.infrastructure.database.models import TaskFolderLinkRecord, TaskRecord
from officeflow.infrastructure.database.session import SessionFactory


class SqlAlchemyFolderLinkRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self.sessions = sessions

    @staticmethod
    def _value(row: TaskFolderLinkRecord) -> FolderLink:
        return FolderLink(row.id, row.task_id, row.name, row.path, row.position, row.created_at)

    def list(self, task_id: int) -> tuple[FolderLink, ...]:
        with self.sessions.transaction() as session:
            return tuple(
                self._value(row)
                for row in session.scalars(
                    select(TaskFolderLinkRecord)
                    .where(TaskFolderLinkRecord.task_id == task_id)
                    .order_by(TaskFolderLinkRecord.position, TaskFolderLinkRecord.id)
                )
            )

    def save(
        self,
        task_id: int,
        name: str,
        path: str,
        identity: int | None,
        expected: FolderLink | None = None,
    ) -> FolderLink:
        with self.sessions.write_unit_of_work() as unit, unit.transaction() as session:
            task = session.get(TaskRecord, task_id)
            if task is None or task.deleted_at is not None:
                raise ValueError("휴지통 업무는 복원한 뒤 폴더를 변경하세요.")
            rows = list(
                session.scalars(
                    select(TaskFolderLinkRecord).where(TaskFolderLinkRecord.task_id == task_id)
                )
            )
            if any(row.id != identity and row.path.casefold() == path.casefold() for row in rows):
                raise ValueError("이미 연결한 폴더입니다.")
            if identity is None:
                if len(rows) >= 50:
                    raise ValueError("한 업무에 폴더는 최대 50개 연결할 수 있습니다.")
                row = TaskFolderLinkRecord(
                    task_id=task_id,
                    name=name,
                    path=path,
                    position=max((row.position for row in rows), default=-1) + 1,
                    created_at=datetime.now(UTC),
                )
                session.add(row)
            else:
                found = next((row for row in rows if row.id == identity), None)
                if found is None:
                    raise LookupError("폴더 연결을 찾을 수 없습니다. 다시 조회하세요.")
                if expected is not None and self._value(found) != expected:
                    raise ValueError(
                        "다른 창에서 폴더 연결을 변경했습니다. 다시 조회한 뒤 변경하세요."
                    )
                row = found
                row.name, row.path = name, path
            session.flush()
            return self._value(row)

    def delete(self, task_id: int, identity: int) -> None:
        with self.sessions.write_unit_of_work() as unit, unit.transaction() as session:
            task = session.get(TaskRecord, task_id)
            row = session.get(TaskFolderLinkRecord, identity)
            if task is None or task.deleted_at is not None:
                raise ValueError("휴지통 업무는 복원한 뒤 연결을 삭제하세요.")
            if row is None or row.task_id != task_id:
                raise LookupError("폴더 연결을 찾을 수 없습니다.")
            session.delete(row)
