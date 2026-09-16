from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from officeflow import __version__
from officeflow.application.migration import (
    LegacyMigrationError,
    MigrationCounts,
    MigrationIssue,
    MigrationPreview,
    MigrationResult,
)
from officeflow.bootstrap.paths import AppPaths
from officeflow.domain.enums import TaskPriority, TaskStatus
from officeflow.domain.recurrence import recurrence_until_utc
from officeflow.infrastructure.attachments.storage import ManagedAttachmentStorage
from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.models import (
    AppSettingRecord,
    AttachmentRecord,
    NoteRecord,
    ReminderRecord,
    TaskOccurrenceRecord,
    TaskRecord,
    WorkLogRecord,
)
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine

_DATE_FORMATS = ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d")
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)
_TIME_FORMATS = ("%H:%M", "%H:%M:%S")
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_SAFE_EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,16}$")


@dataclass(frozen=True, slots=True)
class _ConvertedTask:
    legacy_id: int
    title: str
    description: str
    status: TaskStatus
    priority: TaskPriority
    all_day: bool
    starts_at: datetime | None
    ends_at: datetime | None
    timezone: str
    recurrence_rule: str | None
    result_note: str
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    related_path: str


@dataclass(frozen=True, slots=True)
class _AttachmentCandidate:
    task_legacy_id: int
    original_name: str
    raw_path: str
    created_at: datetime


class LegacyV26Migration:
    def __init__(
        self,
        paths: AppPaths,
        backup_manager: BackupManager,
        *,
        timezone: str = "Asia/Seoul",
    ) -> None:
        self._paths = paths
        self._backup_manager = backup_manager
        self._timezone = timezone
        self._zone = ZoneInfo(timezone)

    def preview(
        self,
        source_database: Path,
        attachment_root: Path | None = None,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> MigrationPreview:
        source = source_database.expanduser().resolve()
        if not source.is_file():
            raise LegacyMigrationError("선택한 v2.6 데이터베이스를 찾을 수 없습니다.")
        if source == self._paths.database_file.resolve():
            raise LegacyMigrationError("현재 OfficeFlow 3.0 데이터베이스는 가져올 수 없습니다.")
        if attachment_root is not None and not attachment_root.expanduser().is_dir():
            raise LegacyMigrationError("선택한 첨부파일 폴더를 찾을 수 없습니다.")
        root = self._attachment_root(source, attachment_root)
        digest = _sha256(source, cancel_requested)
        issues: list[MigrationIssue] = []
        if self._already_imported_source(digest):
            issues.append(
                MigrationIssue(
                    "error",
                    "source_already_imported",
                    "이 v2.6 데이터베이스는 이미 가져왔습니다.",
                )
            )
        if source.with_name(source.name + "-wal").exists():
            issues.append(
                MigrationIssue(
                    "error",
                    "source_wal_present",
                    "v2.6이 실행 중이거나 종료 처리가 남아 있습니다. v2.6을 종료한 뒤 "
                    "DB 옆의 -wal 파일이 사라졌는지 확인하세요.",
                )
            )
        with _open_legacy(source) as connection:
            tables = _table_columns(connection)
            self._validate_schema(connection, tables)
            counts = MigrationCounts(
                tasks=_table_count(connection, "tasks"),
                work_logs=_table_count(connection, "history_logs"),
                notes=_table_count(connection, "memos"),
                attachments=_table_count(connection, "attachments"),
            )
            existing = self._existing_legacy_ids()
            task_rows = connection.execute("SELECT * FROM tasks ORDER BY id").fetchall()
            task_ids = {int(row["id"]) for row in task_rows}
            importable_tasks = 0
            importable_related_files = 0
            for row in task_rows:
                _raise_if_canceled(cancel_requested)
                legacy_id = int(row["id"])
                if legacy_id in existing:
                    issues.append(
                        MigrationIssue(
                            "info",
                            "already_imported",
                            "이미 가져온 업무이므로 건너뜁니다.",
                            "tasks",
                            legacy_id,
                        )
                    )
                    continue
                importable_tasks += 1
                self._inspect_task(row, tables["tasks"], issues)
                related_path = _text(_row_value(row, "file_path"))
                if related_path:
                    resolved = _resolve_legacy_path(related_path, source.parent, root)
                    if resolved is None:
                        issues.append(
                            MigrationIssue(
                                "info",
                                "related_path_preserved",
                                "관련 파일 또는 폴더를 찾을 수 없어 설명에 경로만 보존합니다: "
                                f"{related_path}",
                                "tasks",
                                legacy_id,
                            )
                        )
                    else:
                        importable_related_files += 1

            orphan_logs = 0
            importable_logs = 0
            if "history_logs" in tables:
                for row in connection.execute("SELECT * FROM history_logs ORDER BY id"):
                    task_id = _optional_int(_row_value(row, "task_id"))
                    if task_id in existing:
                        continue
                    importable_logs += 1
                    if task_id is not None and task_id not in task_ids:
                        orphan_logs += 1
                        issues.append(
                            MigrationIssue(
                                "warning",
                                "orphan_work_log",
                                "연결된 원본 업무가 없어 독립 업무일지로 가져옵니다.",
                                "history_logs",
                                int(row["id"]),
                            )
                        )
                    if _parse_date(_text(_row_value(row, "date"))) is None:
                        issues.append(
                            MigrationIssue(
                                "warning",
                                "invalid_log_date",
                                "업무일지 날짜를 해석할 수 없어 가져온 날짜를 사용합니다.",
                                "history_logs",
                                int(row["id"]),
                            )
                        )

            missing_attachments = 0
            orphan_attachments = 0
            importable_attachments = 0
            if "attachments" in tables:
                for row in connection.execute("SELECT * FROM attachments ORDER BY id"):
                    _raise_if_canceled(cancel_requested)
                    attachment_id = int(row["id"])
                    task_id = _optional_int(_row_value(row, "task_id"))
                    if task_id is None or task_id not in task_ids:
                        orphan_attachments += 1
                        issues.append(
                            MigrationIssue(
                                "warning",
                                "orphan_attachment",
                                "연결된 원본 업무가 없어 첨부파일을 건너뜁니다.",
                                "attachments",
                                attachment_id,
                            )
                        )
                        continue
                    if task_id in existing:
                        continue
                    importable_attachments += 1
                    raw_path = _text(_row_value(row, "saved_path"))
                    if _resolve_legacy_path(raw_path, source.parent, root) is None:
                        missing_attachments += 1
                        issues.append(
                            MigrationIssue(
                                "warning",
                                "missing_attachment",
                                f"첨부파일을 찾을 수 없습니다: {raw_path or '(경로 없음)'}",
                                "attachments",
                                attachment_id,
                            )
                        )

            note_count = 0
            if "memos" in tables:
                note_count = sum(
                    1
                    for row in connection.execute("SELECT * FROM memos")
                    if _text(_row_value(row, "content")).strip()
                )
            importable = MigrationCounts(
                tasks=importable_tasks,
                work_logs=importable_logs,
                notes=note_count,
                attachments=importable_attachments + importable_related_files,
            )
        return MigrationPreview(
            source_database=source,
            source_sha256=digest,
            attachment_root=root,
            source_counts=counts,
            importable_counts=importable,
            missing_attachments=missing_attachments,
            orphan_work_logs=orphan_logs,
            orphan_attachments=orphan_attachments,
            issues=tuple(issues),
        )

    def migrate(
        self,
        preview: MigrationPreview,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> MigrationResult:
        if not preview.can_import:
            raise LegacyMigrationError("검사 오류가 있는 v2.6 데이터베이스는 가져올 수 없습니다.")
        if _sha256(preview.source_database, cancel_requested) != preview.source_sha256:
            raise LegacyMigrationError(
                "미리보기 이후 v2.6 데이터베이스가 변경되었습니다. 다시 검사하세요."
            )
        self._paths.ensure_directories()
        source_backup = self._backup_legacy_source(preview.source_database, cancel_requested)
        issues = list(preview.issues)
        started_at = datetime.now(UTC)
        with tempfile.TemporaryDirectory(prefix=".legacy-import-", dir=self._paths.root) as name:
            stage_paths = AppPaths(Path(name) / "payload")
            stage_paths.ensure_directories()
            self._clone_current_data(stage_paths, cancel_requested)
            upgrade_database(stage_paths.database_file)
            imported, skipped, missing_attachments = self._convert_into_stage(
                source_backup,
                preview.source_sha256,
                preview.source_database.parent,
                preview.attachment_root,
                stage_paths,
                issues,
                cancel_requested,
            )
            self._verify_stage(stage_paths, imported)
            stage_manager = BackupManager(stage_paths)
            archive = stage_manager.create_backup(
                reason="pre-import", cancel_requested=cancel_requested
            )
            report = self._write_report(
                preview=preview,
                source_backup=source_backup,
                imported=imported,
                skipped=skipped,
                missing_attachments=missing_attachments,
                issues=issues,
                started_at=started_at,
                completed_at=datetime.now(UTC),
            )
            try:
                self._backup_manager.stage_restore(
                    archive.path, cancel_requested=cancel_requested
                )
            except Exception:
                report.unlink(missing_ok=True)
                raise
        return MigrationResult(
            source_backup=source_backup,
            pending_restore=self._paths.pending_restore_file,
            report_path=report,
            imported_counts=imported,
            skipped_tasks=skipped,
            missing_attachments=missing_attachments,
        )

    def _validate_schema(
        self, connection: sqlite3.Connection, tables: dict[str, frozenset[str]]
    ) -> None:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise LegacyMigrationError("v2.6 데이터베이스 무결성 검사에 실패했습니다.")
        task_columns = tables.get("tasks", frozenset())
        required = {"id", "content"}
        if not required <= task_columns:
            raise LegacyMigrationError(
                "v2.6 tasks 테이블 또는 필수 열(id, content)을 찾을 수 없습니다."
            )
        if "title" in task_columns:
            raise LegacyMigrationError("선택한 파일은 v2.6 데이터베이스가 아닙니다.")

    def _inspect_task(
        self,
        row: sqlite3.Row,
        columns: frozenset[str],
        issues: list[MigrationIssue],
    ) -> None:
        legacy_id = int(row["id"])
        content = _text(_row_value(row, "content"))
        first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
        if len(first_line) > 80:
            issues.append(
                MigrationIssue(
                    "info",
                    "title_truncated",
                    "제목은 80자로 줄이고 전체 원문은 설명에 보존합니다.",
                    "tasks",
                    legacy_id,
                )
            )
        target = _text(_row_value(row, "target_date"))
        alarm = _text(_row_value(row, "alarm_time"))
        frequency = _text(_row_value(row, "frequency", "Once")) or "Once"
        if target and _parse_date(target) is None:
            issues.append(
                MigrationIssue(
                    "warning",
                    "invalid_task_date",
                    f"일정 날짜를 해석할 수 없어 일정 없음으로 가져옵니다: {target}",
                    "tasks",
                    legacy_id,
                )
            )
        if alarm and _parse_time(alarm) is None:
            issues.append(
                MigrationIssue(
                    "warning",
                    "invalid_alarm_time",
                    f"알림 시각을 해석할 수 없어 종일 일정으로 가져옵니다: {alarm}",
                    "tasks",
                    legacy_id,
                )
            )
        if frequency.casefold() not in {"once", "daily", "weekly"}:
            issues.append(
                MigrationIssue(
                    "warning",
                    "unknown_frequency",
                    f"알 수 없는 반복 형식은 반복 없음으로 가져옵니다: {frequency}",
                    "tasks",
                    legacy_id,
                )
            )
        deadline = _text(_row_value(row, "deadline")) if "deadline" in columns else ""
        if deadline:
            issues.append(
                MigrationIssue(
                    "info",
                    "legacy_deadline_preserved",
                    f"기존 마감일을 일정 종료일로 추정하지 않고 설명에 보존합니다: {deadline}",
                    "tasks",
                    legacy_id,
                )
            )
        for field, label in (
            ("created_at", "생성 시각"),
            ("updated_at", "수정 시각"),
            ("completed_at", "완료 시각"),
        ):
            raw = _text(_row_value(row, field)) if field in columns else ""
            if raw and _parse_datetime(raw, self._zone) is None:
                issues.append(
                    MigrationIssue(
                        "warning",
                        f"invalid_{field}",
                        f"{label}을 해석할 수 없어 대체 시각을 사용합니다: {raw}",
                        "tasks",
                        legacy_id,
                    )
                )

    def _existing_legacy_ids(self) -> frozenset[int]:
        if not self._paths.database_file.is_file():
            return frozenset()
        try:
            with closing(
                sqlite3.connect(_read_only_uri(self._paths.database_file), uri=True)
            ) as db:
                rows = db.execute(
                    "SELECT legacy_id FROM tasks WHERE legacy_id IS NOT NULL"
                ).fetchall()
                return frozenset(int(row[0]) for row in rows)
        except sqlite3.Error:
            return frozenset()

    def _already_imported_source(self, digest: str) -> bool:
        if not self._paths.database_file.is_file():
            return False
        try:
            with closing(
                sqlite3.connect(_read_only_uri(self._paths.database_file), uri=True)
            ) as db:
                row = db.execute(
                    "SELECT 1 FROM app_settings WHERE key=?",
                    (f"legacy_import:{digest}",),
                ).fetchone()
                return row is not None
        except sqlite3.Error:
            return False

    def _backup_legacy_source(
        self,
        source: Path,
        cancel_requested: Callable[[], bool] | None,
    ) -> Path:
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        target = self._paths.backup_dir / (
            f"officeflow-v26-source-{stamp}-{uuid.uuid4().hex[:8]}.db"
        )
        temporary = target.with_suffix(".part")
        try:
            with (
                closing(sqlite3.connect(_read_only_uri(source), uri=True)) as input_db,
                closing(sqlite3.connect(temporary)) as output_db,
            ):
                input_db.backup(
                    output_db,
                    pages=256,
                    progress=lambda _status, _remaining, _total: _raise_if_canceled(
                        cancel_requested
                    ),
                )
            os.replace(temporary, target)
        except (OSError, sqlite3.Error) as error:
            raise LegacyMigrationError(
                f"v2.6 원본 안전 사본을 만들지 못했습니다: {error}"
            ) from error
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def _clone_current_data(
        self,
        stage: AppPaths,
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        if self._paths.database_file.is_file():
            with (
                closing(sqlite3.connect(self._paths.database_file)) as source_connection,
                closing(sqlite3.connect(stage.database_file)) as target,
            ):
                source_connection.backup(
                    target,
                    pages=256,
                    progress=lambda _status, _remaining, _total: _raise_if_canceled(
                        cancel_requested
                    ),
                )
        if self._paths.attachment_dir.exists():
            for source_path in self._paths.attachment_dir.rglob("*"):
                _raise_if_canceled(cancel_requested)
                relative = source_path.relative_to(self._paths.attachment_dir)
                if any(part.startswith(".") for part in relative.parts):
                    continue
                if source_path.is_file():
                    destination = stage.attachment_dir / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _copy_file(source_path, destination, cancel_requested)
        if self._paths.settings_file.is_file():
            _copy_file(self._paths.settings_file, stage.settings_file, cancel_requested)

    def _convert_into_stage(
        self,
        source_database: Path,
        source_digest: str,
        legacy_source_dir: Path,
        attachment_root: Path | None,
        stage: AppPaths,
        issues: list[MigrationIssue],
        cancel_requested: Callable[[], bool] | None,
    ) -> tuple[MigrationCounts, int, int]:
        engine = create_database_engine(stage.database_file)
        sessions = SessionFactory(engine)
        storage = ManagedAttachmentStorage(stage.attachment_dir)
        imported_tasks = 0
        imported_logs = 0
        imported_notes = 0
        imported_attachments = 0
        missing_attachments = 0
        skipped_tasks = 0
        now = datetime.now(UTC)
        try:
            with _open_legacy(source_database) as source:
                tables = _table_columns(source)
                task_rows = source.execute("SELECT * FROM tasks ORDER BY id").fetchall()
                with sessions.transaction() as session:
                    existing = frozenset(
                        int(value)
                        for value in session.scalars(
                            select(TaskRecord.legacy_id).where(TaskRecord.legacy_id.is_not(None))
                        ).all()
                        if value is not None
                    )
                    id_map: dict[int, int] = {}
                    converted_by_legacy: dict[int, _ConvertedTask] = {}
                    related_candidates: list[_AttachmentCandidate] = []
                    for row in task_rows:
                        _raise_if_canceled(cancel_requested)
                        legacy_id = int(row["id"])
                        if legacy_id in existing:
                            skipped_tasks += 1
                            continue
                        converted = self._convert_task(row, tables["tasks"], issues, now)
                        task_record = TaskRecord(
                            legacy_id=legacy_id,
                            title=converted.title,
                            description=converted.description,
                            status=converted.status.value,
                            priority=converted.priority.value,
                            is_pinned=False,
                            all_day=converted.all_day,
                            starts_at=converted.starts_at,
                            ends_at=converted.ends_at,
                            timezone=converted.timezone,
                            recurrence_rule=converted.recurrence_rule,
                            recurrence_until=recurrence_until_utc(converted.recurrence_rule),
                            result_note=converted.result_note,
                            completed_at=converted.completed_at,
                            created_at=converted.created_at,
                            updated_at=converted.updated_at,
                            deleted_at=None,
                        )
                        session.add(task_record)
                        session.flush()
                        id_map[legacy_id] = task_record.id
                        converted_by_legacy[legacy_id] = converted
                        imported_tasks += 1
                        if converted.starts_at is not None and not converted.all_day:
                            session.add(
                                ReminderRecord(
                                    task_id=task_record.id,
                                    relation="start",
                                    offset_minutes=0,
                                    absolute_at=None,
                                    enabled=True,
                                    last_fired_key=None,
                                )
                            )
                        if (
                            converted.related_path
                            and _resolve_legacy_path(
                                converted.related_path, legacy_source_dir, attachment_root
                            )
                            is not None
                        ):
                            related_candidates.append(
                                _AttachmentCandidate(
                                    task_legacy_id=legacy_id,
                                    original_name=(
                                        Path(converted.related_path).name
                                        or f"관련 경로 {legacy_id}"
                                    ),
                                    raw_path=converted.related_path,
                                    created_at=converted.created_at,
                                )
                            )

                    occurrence_by_key: dict[tuple[int, date], TaskOccurrenceRecord] = {}
                    if "history_logs" in tables:
                        for row in source.execute("SELECT * FROM history_logs ORDER BY id"):
                            _raise_if_canceled(cancel_requested)
                            legacy_log_id = int(row["id"])
                            legacy_task_id = _optional_int(_row_value(row, "task_id"))
                            if legacy_task_id in existing:
                                continue
                            task_id = (
                                id_map.get(legacy_task_id) if legacy_task_id is not None else None
                            )
                            log_date = _parse_date(_text(_row_value(row, "date")))
                            if log_date is None:
                                log_date = now.astimezone(self._zone).date()
                            priority = _priority(
                                _text(_row_value(row, "category")),
                                issues,
                                "history_logs",
                                legacy_log_id,
                            )
                            content = _text(_row_value(row, "content")).strip() or "가져온 업무일지"
                            result = _text(_row_value(row, "result")).strip()
                            occurrence_id: int | None = None
                            log_task = (
                                converted_by_legacy.get(legacy_task_id)
                                if legacy_task_id is not None
                                else None
                            )
                            if (
                                task_id is not None
                                and log_task is not None
                                and log_task.recurrence_rule is not None
                                and log_task.starts_at is not None
                                and self._log_matches_recurrence(log_task, log_date)
                            ):
                                key = (task_id, log_date)
                                occurrence = occurrence_by_key.get(key)
                                if occurrence is None:
                                    local_time = log_task.starts_at.astimezone(self._zone).time()
                                    occurrence_start = datetime.combine(
                                        log_date, local_time, tzinfo=self._zone
                                    ).astimezone(UTC)
                                    occurrence = TaskOccurrenceRecord(
                                        task_id=task_id,
                                        occurrence_start=occurrence_start,
                                        occurrence_end=None,
                                        effective_start=None,
                                        effective_end=None,
                                        status="completed",
                                        completed_at=occurrence_start,
                                        result_note=result,
                                    )
                                    session.add(occurrence)
                                    session.flush()
                                    occurrence_by_key[key] = occurrence
                                occurrence_id = occurrence.id
                            elif (
                                task_id is not None
                                and log_task is not None
                                and log_task.recurrence_rule is not None
                            ):
                                issues.append(
                                    MigrationIssue(
                                        "warning",
                                        "unmatched_recurrence_log",
                                        "반복 규칙과 날짜가 맞지 않아 발생 완료 대신 업무일지로만 가져옵니다.",
                                        "history_logs",
                                        legacy_log_id,
                                    )
                                )
                            session.add(
                                WorkLogRecord(
                                    task_id=task_id,
                                    occurrence_id=occurrence_id,
                                    log_date=log_date,
                                    content=content,
                                    result=result,
                                    priority_snapshot=priority.value,
                                    created_at=now,
                                    updated_at=now,
                                )
                            )
                            imported_logs += 1

                    if "memos" in tables:
                        for row in source.execute("SELECT * FROM memos ORDER BY id"):
                            content = _text(_row_value(row, "content")).strip()
                            if content:
                                session.add(
                                    NoteRecord(note_date=None, content=content, updated_at=now)
                                )
                                imported_notes += 1

                    candidates = self._attachment_candidates(
                        source,
                        tables,
                        related_candidates,
                        converted_by_legacy,
                    )
                    seen_paths: set[tuple[int, str]] = set()
                    for candidate in candidates:
                        _raise_if_canceled(cancel_requested)
                        task_id = id_map.get(candidate.task_legacy_id)
                        if task_id is None:
                            continue
                        normalized = candidate.raw_path.casefold()
                        dedupe_key = (candidate.task_legacy_id, normalized)
                        if normalized and dedupe_key in seen_paths:
                            issues.append(
                                MigrationIssue(
                                    "info",
                                    "duplicate_attachment_path",
                                    f"같은 경로의 첨부파일을 한 번만 가져옵니다: {candidate.raw_path}",
                                    "attachments",
                                    candidate.task_legacy_id,
                                )
                            )
                            continue
                        seen_paths.add(dedupe_key)
                        source_path = _resolve_legacy_path(
                            candidate.raw_path,
                            legacy_source_dir,
                            attachment_root,
                        )
                        original_name = (candidate.original_name.strip() or "가져온 첨부파일")[:500]
                        if source_path is not None:
                            stored = storage.import_file(
                                source_path, cancel_requested=cancel_requested
                            )
                            attachment_record = AttachmentRecord(
                                task_id=task_id,
                                original_name=original_name,
                                stored_name=stored.stored_name,
                                relative_path=stored.relative_path,
                                size_bytes=stored.size_bytes,
                                checksum=stored.checksum,
                                created_at=candidate.created_at,
                                missing_at=None,
                            )
                        else:
                            missing_attachments += 1
                            if not any(
                                issue.code == "missing_attachment"
                                and issue.legacy_id == candidate.task_legacy_id
                                and candidate.raw_path in issue.message
                                for issue in issues
                            ):
                                issues.append(
                                    MigrationIssue(
                                        "warning",
                                        "missing_attachment_during_import",
                                        "변환 중 첨부파일을 찾을 수 없어 누락 상태로 "
                                        f"기록합니다: {candidate.raw_path or '(경로 없음)'}",
                                        "attachments",
                                        candidate.task_legacy_id,
                                    )
                                )
                            stored_name, relative_path = _missing_storage_path(original_name)
                            attachment_record = AttachmentRecord(
                                task_id=task_id,
                                original_name=original_name,
                                stored_name=stored_name,
                                relative_path=relative_path,
                                size_bytes=0,
                                checksum=None,
                                created_at=candidate.created_at,
                                missing_at=now,
                            )
                        session.add(attachment_record)
                        imported_attachments += 1
                    session.add(
                        AppSettingRecord(
                            key=f"legacy_import:{source_digest}",
                            value_json=json.dumps(
                                {
                                    "source_backup": str(source_database),
                                    "imported_at": now.isoformat(),
                                    "tasks": imported_tasks,
                                },
                                ensure_ascii=False,
                            ),
                            updated_at=now,
                        )
                    )
        except (OSError, sqlite3.Error, ValueError) as error:
            raise LegacyMigrationError(f"v2.6 데이터를 변환하지 못했습니다: {error}") from error
        finally:
            engine.dispose()
        return (
            MigrationCounts(
                tasks=imported_tasks,
                work_logs=imported_logs,
                notes=imported_notes,
                attachments=imported_attachments,
            ),
            skipped_tasks,
            missing_attachments,
        )

    def _convert_task(
        self,
        row: sqlite3.Row,
        columns: frozenset[str],
        issues: list[MigrationIssue],
        now: datetime,
    ) -> _ConvertedTask:
        legacy_id = int(row["id"])
        content = _text(_row_value(row, "content"))
        title = _legacy_title(content, legacy_id)
        description = content.strip()
        deadline = _text(_row_value(row, "deadline")) if "deadline" in columns else ""
        related_path = _text(_row_value(row, "file_path")) if "file_path" in columns else ""
        preserved: list[str] = []
        if deadline:
            preserved.append(f"기존 마감일: {deadline}")
        if related_path:
            preserved.append(f"기존 관련 경로: {related_path}")
        if preserved:
            description = f"{description}\n\n" if description else ""
            description += "\n".join(preserved)

        target = _parse_date(_text(_row_value(row, "target_date")))
        alarm = _parse_time(_text(_row_value(row, "alarm_time")))
        starts_at: datetime | None = None
        ends_at: datetime | None = None
        all_day = False
        if target is not None:
            if alarm is not None:
                starts_at = datetime.combine(target, alarm, tzinfo=self._zone).astimezone(UTC)
            else:
                all_day = True
                local_start = datetime.combine(target, time.min, tzinfo=self._zone)
                starts_at = local_start.astimezone(UTC)
                ends_at = (local_start + timedelta(days=1)).astimezone(UTC)

        frequency = _text(_row_value(row, "frequency", "Once")) or "Once"
        recurrence_rule: str | None = None
        if starts_at is not None and frequency.casefold() == "daily":
            recurrence_rule = "FREQ=DAILY;INTERVAL=1"
        elif starts_at is not None and frequency.casefold() == "weekly":
            weekday = _optional_int(
                _row_value(
                    row, "weekday", _row_value(row, "week_day", target.weekday() if target else 0)
                )
            )
            if weekday is None or not 0 <= weekday <= 6:
                weekday = target.weekday() if target is not None else 0
                issues.append(
                    MigrationIssue(
                        "warning",
                        "invalid_weekday",
                        "주간 반복 요일이 잘못되어 일정 날짜의 요일을 사용합니다.",
                        "tasks",
                        legacy_id,
                    )
                )
            recurrence_rule = f"FREQ=WEEKLY;INTERVAL=1;BYDAY={_WEEKDAYS[weekday]}"
        elif frequency.casefold() not in {"once", "daily", "weekly"}:
            recurrence_rule = None

        status = _status(_text(_row_value(row, "status", "active")), issues, legacy_id)
        priority = _priority(_text(_row_value(row, "category", "보통")), issues, "tasks", legacy_id)
        created = _parse_datetime(_text(_row_value(row, "created_at")), self._zone) or now
        updated = _parse_datetime(_text(_row_value(row, "updated_at")), self._zone) or created
        completed = _parse_datetime(_text(_row_value(row, "completed_at")), self._zone)
        if status is TaskStatus.COMPLETED and completed is None:
            completed = updated
        return _ConvertedTask(
            legacy_id=legacy_id,
            title=title,
            description=description,
            status=status,
            priority=priority,
            all_day=all_day,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone=self._timezone,
            recurrence_rule=recurrence_rule,
            result_note=_text(_row_value(row, "result_memo")).strip(),
            completed_at=completed,
            created_at=created,
            updated_at=updated,
            related_path=related_path,
        )

    def _attachment_candidates(
        self,
        source: sqlite3.Connection,
        tables: dict[str, frozenset[str]],
        related: list[_AttachmentCandidate],
        converted: dict[int, _ConvertedTask],
    ) -> tuple[_AttachmentCandidate, ...]:
        candidates: list[_AttachmentCandidate] = []
        if "attachments" in tables:
            for row in source.execute("SELECT * FROM attachments ORDER BY id"):
                task_id = _optional_int(_row_value(row, "task_id"))
                if task_id is None or task_id not in converted:
                    continue
                created = (
                    _parse_datetime(_text(_row_value(row, "upload_date")), self._zone)
                    or converted[task_id].created_at
                )
                candidates.append(
                    _AttachmentCandidate(
                        task_legacy_id=task_id,
                        original_name=(
                            _text(_row_value(row, "file_name"))
                            or Path(_text(_row_value(row, "saved_path"))).name
                            or f"가져온 첨부파일 {int(row['id'])}"
                        ),
                        raw_path=_text(_row_value(row, "saved_path")),
                        created_at=created,
                    )
                )
        candidates.extend(related)
        return tuple(candidates)

    @staticmethod
    def _log_matches_recurrence(converted: _ConvertedTask, log_date: date) -> bool:
        rule = converted.recurrence_rule or ""
        if rule.startswith("FREQ=DAILY"):
            return True
        if "BYDAY=" not in rule:
            return False
        day = rule.split("BYDAY=", maxsplit=1)[1].split(";", maxsplit=1)[0]
        return day == _WEEKDAYS[log_date.weekday()]

    @staticmethod
    def _verify_stage(stage: AppPaths, imported: MigrationCounts) -> None:
        try:
            with closing(sqlite3.connect(stage.database_file)) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise LegacyMigrationError("변환 DB 무결성 검사에 실패했습니다.")
                foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
                if foreign_keys:
                    raise LegacyMigrationError(
                        f"변환 DB에 잘못된 연결 관계가 {len(foreign_keys)}건 있습니다."
                    )
                imported_legacy = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM tasks WHERE legacy_id IS NOT NULL"
                    ).fetchone()[0]
                )
                if imported_legacy < imported.tasks:
                    raise LegacyMigrationError("변환된 업무 개수 검증에 실패했습니다.")
                attachment_rows = connection.execute(
                    """
                    SELECT a.relative_path, a.size_bytes, a.checksum, a.missing_at
                    FROM attachments AS a
                    JOIN tasks AS t ON t.id = a.task_id
                    WHERE t.legacy_id IS NOT NULL
                    """
                ).fetchall()
                if len(attachment_rows) < imported.attachments:
                    raise LegacyMigrationError("변환된 첨부파일 개수 검증에 실패했습니다.")
                attachment_root = stage.attachment_dir.resolve()
                for relative, size, checksum, missing_at in attachment_rows:
                    if missing_at is not None:
                        continue
                    candidate = (stage.attachment_dir / str(relative)).resolve()
                    try:
                        candidate.relative_to(attachment_root)
                    except ValueError as error:
                        raise LegacyMigrationError(
                            "변환된 첨부파일 경로가 안전하지 않습니다."
                        ) from error
                    if not candidate.is_file() or candidate.stat().st_size != int(size):
                        raise LegacyMigrationError(
                            f"변환된 첨부파일을 검증하지 못했습니다: {relative}"
                        )
                    if checksum and _sha256(candidate, None) != str(checksum):
                        raise LegacyMigrationError(
                            f"변환된 첨부파일 체크섬이 일치하지 않습니다: {relative}"
                        )
        except sqlite3.Error as error:
            raise LegacyMigrationError(f"변환 DB를 검증하지 못했습니다: {error}") from error

    def _write_report(
        self,
        *,
        preview: MigrationPreview,
        source_backup: Path,
        imported: MigrationCounts,
        skipped: int,
        missing_attachments: int,
        issues: list[MigrationIssue],
        started_at: datetime,
        completed_at: datetime,
    ) -> Path:
        stamp = completed_at.astimezone().strftime("%Y%m%d-%H%M%S")
        path = self._paths.migration_report_dir / (
            f"v26-import-{stamp}-{uuid.uuid4().hex[:8]}.json"
        )
        temporary = path.with_suffix(".part")
        payload = {
            "format_version": 1,
            "app_version": _app_version(),
            "source_database": str(preview.source_database),
            "source_backup": str(source_backup),
            "source_sha256": preview.source_sha256,
            "attachment_root": (
                str(preview.attachment_root) if preview.attachment_root is not None else None
            ),
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "source_counts": asdict(preview.source_counts),
            "importable_counts": asdict(preview.importable_counts),
            "imported_counts": asdict(imported),
            "skipped_tasks": skipped,
            "missing_attachments": missing_attachments,
            "orphan_work_logs": preview.orphan_work_logs,
            "orphan_attachments": preview.orphan_attachments,
            "issues": [asdict(issue) for issue in issues],
            "pending_restore": str(self._paths.pending_restore_file),
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary, path)
        except OSError as error:
            raise LegacyMigrationError(f"변환 보고서를 저장하지 못했습니다: {error}") from error
        finally:
            temporary.unlink(missing_ok=True)
        return path

    @staticmethod
    def _attachment_root(source: Path, requested: Path | None) -> Path | None:
        if requested is not None:
            root = requested.expanduser().resolve()
            return root if root.is_dir() else None
        candidate = source.parent / "saved_files"
        return candidate.resolve() if candidate.is_dir() else None


def _open_legacy(path: Path) -> closing[sqlite3.Connection]:
    try:
        connection = sqlite3.connect(_read_only_uri(path), uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return closing(connection)
    except sqlite3.Error as error:
        raise LegacyMigrationError(f"v2.6 데이터베이스를 열지 못했습니다: {error}") from error


def _read_only_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def _table_columns(connection: sqlite3.Connection) -> dict[str, frozenset[str]]:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    result: dict[str, frozenset[str]] = {}
    for table in tables:
        quoted = table.replace('"', '""')
        result[table] = frozenset(
            str(row[1]) for row in connection.execute(f'PRAGMA table_info("{quoted}")')
        )
    return result


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    available = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if available is None:
        return 0
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except IndexError:
        return default


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    return None


def _parse_time(value: str) -> time | None:
    if not value:
        return None
    for pattern in _TIME_FORMATS:
        try:
            return datetime.strptime(value, pattern).time()
        except ValueError:
            continue
    return None


def _parse_datetime(value: str, zone: ZoneInfo) -> datetime | None:
    if not value:
        return None
    for pattern in _DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(value, pattern)
            return parsed.replace(tzinfo=zone).astimezone(UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _legacy_title(content: str, legacy_id: int) -> str:
    first = next((line.strip() for line in content.splitlines() if line.strip()), "")
    return first[:80] if first else f"가져온 업무 {legacy_id}"


def _status(value: str, issues: list[MigrationIssue], legacy_id: int) -> TaskStatus:
    normalized = value.casefold()
    mapping = {
        "active": TaskStatus.ACTIVE,
        "pending": TaskStatus.PENDING,
        "done": TaskStatus.COMPLETED,
        "completed": TaskStatus.COMPLETED,
        "canceled": TaskStatus.CANCELED,
        "cancelled": TaskStatus.CANCELED,
    }
    if normalized in mapping:
        return mapping[normalized]
    issues.append(
        MigrationIssue(
            "warning",
            "unknown_status",
            f"알 수 없는 상태를 진행으로 가져옵니다: {value or '(빈 값)'}",
            "tasks",
            legacy_id,
        )
    )
    return TaskStatus.ACTIVE


def _priority(
    value: str,
    issues: list[MigrationIssue],
    table: str,
    legacy_id: int,
) -> TaskPriority:
    mapping = {
        "보통": TaskPriority.NORMAL,
        "관심": TaskPriority.ATTENTION,
        "중요": TaskPriority.IMPORTANT,
        "긴급": TaskPriority.URGENT,
        "normal": TaskPriority.NORMAL,
        "attention": TaskPriority.ATTENTION,
        "important": TaskPriority.IMPORTANT,
        "urgent": TaskPriority.URGENT,
    }
    normalized = value.casefold()
    if normalized in mapping:
        return mapping[normalized]
    issues.append(
        MigrationIssue(
            "warning",
            "unknown_priority",
            f"알 수 없는 중요도를 보통으로 가져옵니다: {value or '(빈 값)'}",
            table,
            legacy_id,
        )
    )
    return TaskPriority.NORMAL


def _resolve_legacy_path(
    raw_path: str,
    source_path: Path,
    attachment_root: Path | None,
) -> Path | None:
    if not raw_path:
        return None
    raw = Path(raw_path).expanduser()
    candidates: list[Path] = [raw] if raw.is_absolute() else [source_path / raw]
    if attachment_root is not None:
        candidates.extend((attachment_root / raw, attachment_root / raw.name))
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, FileNotFoundError):
            continue
        if resolved.is_file():
            return resolved
    return None


def _missing_storage_path(original_name: str) -> tuple[str, str]:
    token = uuid.uuid4().hex
    suffix = Path(original_name).suffix
    safe_suffix = suffix.lower() if _SAFE_EXTENSION.fullmatch(suffix) else ""
    stored_name = f"{token}{safe_suffix}"
    return stored_name, f"{token[:2]}/{token[2:4]}/{stored_name}"


def _sha256(path: Path, cancel_requested: Callable[[], bool] | None) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                _raise_if_canceled(cancel_requested)
                digest.update(chunk)
    except OSError as error:
        raise LegacyMigrationError(f"v2.6 데이터베이스를 읽지 못했습니다: {error}") from error
    return digest.hexdigest()


def _copy_file(
    source: Path,
    destination: Path,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
            while chunk := input_stream.read(1024 * 1024):
                _raise_if_canceled(cancel_requested)
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _raise_if_canceled(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise LegacyMigrationError("v2.6 데이터 가져오기를 취소했습니다.")


def _app_version() -> str:
    try:
        return version("officeflow")
    except PackageNotFoundError:
        return __version__
