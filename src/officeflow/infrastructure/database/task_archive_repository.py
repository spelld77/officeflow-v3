from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.sql import Select

from officeflow.application.attachment_export import FileReference
from officeflow.application.task_archive import (
    ArchiveFile,
    ArchiveOptions,
    ArchiveScope,
    ArchiveTask,
)
from officeflow.infrastructure.database.models import (
    AttachmentRecord,
    AttachmentVersionRecord,
    ChecklistItemRecord,
    SnapshotFileRecord,
    TaskFolderLinkRecord,
    TaskOccurrenceRecord,
    TaskRecord,
    TaskSnapshotRecord,
    WorkLogRecord,
)
from officeflow.infrastructure.database.session import SessionFactory


def display(value: date | datetime | None, timezone: str) -> str:
    if value is None:
        return "없음"
    if isinstance(value, datetime):
        value = value.astimezone(ZoneInfo(timezone))
    day = "월화수목금토일"[value.weekday()]
    return f"{value:%Y-%m-%d}({day})" + (f" {value:%H:%M}" if isinstance(value, datetime) else "")


class SqlAlchemyArchiveRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self.sessions = sessions

    def task_ids(self, options: ArchiveOptions) -> tuple[int, ...]:
        with self.sessions.read_snapshot() as unit, unit.transaction() as session:
            query = select(TaskRecord.id).where(TaskRecord.deleted_at.is_(None))
            if options.scope == ArchiveScope.SELECTED:
                selected = tuple(dict.fromkeys(options.task_ids))
                # Keep SQLite parameter counts bounded even for a 5,000-task selection.
                found: set[int] = set()
                for offset in range(0, len(selected), 400):
                    found.update(
                        session.scalars(
                            query.where(TaskRecord.id.in_(selected[offset : offset + 400]))
                        )
                    )
                if found != set(selected):
                    raise ValueError(
                        "선택 업무 일부가 삭제/휴지통 이동되었습니다. 다시 선택하세요."
                    )
                return selected
            column = getattr(TaskRecord, options.basis.value)
            zone = ZoneInfo(options.timezone)
            start = datetime.combine(options.start, time.min, zone)
            end = datetime.combine(options.end + timedelta(days=1), time.min, zone)
            query = query.where(column >= start, column < end)
            if options.completed_only:
                query = query.where(TaskRecord.status == "completed")
            identities = tuple(session.scalars(query.order_by(TaskRecord.id).limit(5001)))
            if len(identities) > 5000:
                raise ValueError("5,000개를 초과합니다. 보관 기간을 나누세요.")
            return identities

    def capture(self, task_id: int, options: ArchiveOptions) -> ArchiveTask:
        with self.sessions.read_snapshot() as unit, unit.transaction() as session:
            task = session.get(TaskRecord, task_id)
            if task is None or task.deleted_at is not None:
                raise LookupError("보관할 업무가 삭제/휴지통 이동되었습니다.")

            def rows[T](query: Select[tuple[T]]) -> list[T]:
                result = list(session.scalars(query.limit(5001)))
                if len(result) > 5000:
                    raise ValueError(
                        f"업무 {task_id}에 자료가 너무 많습니다. 개별 자료를 먼저 외부 저장하세요."
                    )
                return result

            sections: list[tuple[str, str]] = []
            for row in rows(
                select(ChecklistItemRecord)
                .where(ChecklistItemRecord.task_id == task_id)
                .order_by(ChecklistItemRecord.position, ChecklistItemRecord.id)
            ):
                sections.append(("체크리스트", f"{'☑' if row.is_done else '☐'} {row.content}"))
            for log in rows(
                select(WorkLogRecord)
                .where(WorkLogRecord.task_id == task_id)
                .order_by(WorkLogRecord.log_date, WorkLogRecord.id)
            ):
                sections.append(
                    (
                        "업무일지",
                        f"{display(log.log_date, options.timezone)}\n{log.content}\n결과: {log.result}",
                    )
                )
            for occurrence in rows(
                select(TaskOccurrenceRecord)
                .where(TaskOccurrenceRecord.task_id == task_id)
                .order_by(TaskOccurrenceRecord.occurrence_start, TaskOccurrenceRecord.id)
            ):
                sections.append(
                    (
                        "저장된 반복 회차",
                        f"원래 회차 {display(occurrence.occurrence_start, options.timezone)} · {occurrence.status}\n"
                        f"이동 시각 {display(occurrence.effective_start, options.timezone)} · 종료 {display(occurrence.effective_end or occurrence.occurrence_end, options.timezone)}\n결과: {occurrence.result_note}",
                    )
                )
            for link in rows(
                select(TaskFolderLinkRecord)
                .where(TaskFolderLinkRecord.task_id == task_id)
                .order_by(TaskFolderLinkRecord.position, TaskFolderLinkRecord.id)
            ):
                sections.append(("외부 폴더 연결 (내용 미포함)", f"{link.name}\n{link.path}"))
            files: list[ArchiveFile] = []
            if options.attachments:
                for attachment in rows(
                    select(AttachmentRecord)
                    .where(
                        AttachmentRecord.task_id == task_id, AttachmentRecord.detached_at.is_(None)
                    )
                    .order_by(AttachmentRecord.sort_order, AttachmentRecord.id)
                ):
                    files.append(
                        ArchiveFile(
                            FileReference.attachment(attachment.id),
                            attachment.display_name,
                            {"general": "일반", "original": "원본", "work": "작업본"}[
                                attachment.role
                            ],
                            attachment.memo,
                            "",
                            (
                                attachment.asset_id,
                                attachment.relative_path,
                                str(attachment.metadata_revision),
                                str(attachment.sort_order),
                            ),
                        )
                    )
            if options.versions:
                for version in rows(
                    select(AttachmentVersionRecord)
                    .where(AttachmentVersionRecord.task_id == task_id)
                    .order_by(AttachmentVersionRecord.created_at, AttachmentVersionRecord.id)
                ):
                    files.append(
                        ArchiveFile(
                            FileReference.version(version.id),
                            version.original_name,
                            "보관본",
                            version.memo,
                            version.label,
                            (
                                version.asset_id,
                                version.asset.relative_path,
                                str(version.metadata_revision),
                                str(version.asset.fixed_checksum),
                            ),
                        )
                    )
            if options.snapshot_files:
                for snapshot_file in rows(
                    select(SnapshotFileRecord)
                    .join(TaskSnapshotRecord)
                    .where(TaskSnapshotRecord.task_id == task_id)
                    .order_by(SnapshotFileRecord.snapshot_id, SnapshotFileRecord.position)
                ):
                    files.append(
                        ArchiveFile(
                            FileReference.snapshot_file(snapshot_file.id),
                            snapshot_file.name,
                            "중요시점 파일",
                            snapshot_file.memo,
                            snapshot_file.snapshot.label,
                            (
                                snapshot_file.asset_id,
                                snapshot_file.asset.relative_path,
                                snapshot_file.fixed_checksum,
                            ),
                        )
                    )
            return ArchiveTask(
                task.id,
                task.title,
                task.status,
                task.priority,
                task.description,
                task.result_note,
                f"{'종일' if task.all_day else '시간 지정'} · 시작 {display(task.starts_at, options.timezone)} · 종료 {display(task.ends_at, options.timezone)}\n"
                f"반복: {task.recurrence_rule or '없음'} · 반복 종료 {display(task.recurrence_until, options.timezone)}",
                f"등록 {display(task.created_at, options.timezone)} · 수정 {display(task.updated_at, options.timezone)} · 완료 {display(task.completed_at, options.timezone)}",
                tuple(sections),
                tuple(files),
            )
