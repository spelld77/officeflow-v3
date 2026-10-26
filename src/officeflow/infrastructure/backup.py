from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from collections.abc import Callable
from contextlib import ExitStack, closing
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from alembic.script import ScriptDirectory

from officeflow.application.attachments import AttachmentOperationError
from officeflow.application.data_inspection import DataInspectionService
from officeflow.application.file_operations import (
    FileInUseError,
    FileJobCanceledError,
    FileOperationCoordinator,
)
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.attachments.coordination import (
    attachment_coordinator,
)
from officeflow.infrastructure.attachments.deletion_storage import DeletionFileStorage
from officeflow.infrastructure.attachments.internal_storage import checked_managed_path
from officeflow.infrastructure.attachments.read_lease import protected_read
from officeflow.infrastructure.attachments.storage import ManagedAttachmentStorage
from officeflow.infrastructure.database.file_references import (
    assert_supported_sqlite_asset_references,
)
from officeflow.infrastructure.database.migrate import migration_config

_FORMAT_VERSION = 3
logger = logging.getLogger(__name__)


class BackupError(RuntimeError):
    """Raised when a backup cannot be created, verified, or restored safely."""


class BackupCanceledError(BackupError):
    """Cooperative cancellation, not a failed backup or restore."""


class BackupSourceIncompleteError(BackupError):
    """Readable current metadata, but required bytes are missing or changed."""


def _safe_attachment_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or "\\" in value or ".." in path.parts:
        raise BackupError("첨부파일 상대 경로가 올바르지 않습니다.")
    return path


def _format_bytes(size_bytes: int) -> str:
    size = float(max(0, size_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size):,} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size_bytes:,} B"


@dataclass(frozen=True, slots=True)
class BackupFile:
    path: str
    size: int
    sha256: str
    category: str = "recovery"
    asset_id: str | None = None


@dataclass(frozen=True, slots=True)
class BackupManifest:
    format_version: int
    created_at: str
    reason: str
    database_sha256: str
    table_counts: dict[str, int]
    attachments: tuple[BackupFile, ...]
    settings: BackupFile | None = None
    missing_attachments: tuple[str, ...] = ()
    recovery_partition: bool = False


@dataclass(frozen=True, slots=True)
class BackupInfo:
    path: Path
    created_at: datetime
    reason: str
    table_counts: dict[str, int]
    attachment_count: int
    missing_attachments: tuple[str, ...] = ()
    maintenance_warning: str = ""


@dataclass(frozen=True, slots=True)
class LargeStoredFile:
    name: str
    size_bytes: int
    orphaned: bool = False
    detached: bool = False


@dataclass(frozen=True, slots=True)
class OrphanAttachmentFile:
    relative_path: str
    name: str
    size_bytes: int
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class DataUsageSnapshot:
    task_count: int
    open_task_count: int
    completed_task_count: int
    archived_task_count: int
    trash_task_count: int
    aged_trash_task_count: int
    reminder_cleanup_candidate_count: int
    linked_attachment_count: int
    linked_attachment_bytes: int
    detached_attachment_count: int
    detached_attachment_bytes: int
    aged_detached_attachment_count: int
    duplicate_attachment_count: int
    duplicate_attachment_bytes: int
    stored_attachment_count: int
    stored_attachment_bytes: int
    missing_attachment_count: int
    orphan_attachment_count: int
    orphan_attachment_bytes: int
    backup_count: int
    backup_bytes: int
    manual_backup_count: int
    automatic_backup_count: int
    latest_automatic_backup_at: datetime | None
    database_bytes: int
    largest_files: tuple[LargeStoredFile, ...]
    quarantine_file_count: int = 0
    quarantine_bytes: int = 0
    unverified_attachment_count: int = 0
    unavailable_attachment_count: int = 0
    history_count: int = 0
    snapshot_count: int = 0
    snapshot_file_count: int = 0

    @property
    def total_bytes(self) -> int:
        return (
            self.database_bytes
            + self.stored_attachment_bytes
            + self.quarantine_bytes
            + self.backup_bytes
        )


class BackupManager:
    def __init__(
        self, paths: AppPaths, *, coordinator: FileOperationCoordinator | None = None
    ) -> None:
        self._paths = paths
        self._coordinator = coordinator or attachment_coordinator(paths.attachment_dir)
        self.restore_warnings: tuple[str, ...] = ()
        from officeflow.infrastructure.backup_health import BackupHealthStore

        self.health = BackupHealthStore(paths.root / "backup-health.json")

    @property
    def diagnostic_directory(self) -> Path:
        return self._paths.log_dir

    def inspection_service(self) -> DataInspectionService:
        from officeflow.infrastructure.data_inspection import (
            ManagedInspectionReader,
            SqliteInspectionRepository,
        )

        return DataInspectionService(
            SqliteInspectionRepository(self._paths.database_file),
            ManagedInspectionReader(self._paths.attachment_dir, self._coordinator),
        )

    def recover_attachment_deletions(self) -> tuple[str, ...]:
        try:
            return self._recover_attachment_deletions()
        except sqlite3.Error as error:
            raise BackupError(
                "삭제 복구의 파일 원장을 읽지 못했습니다. 격리 자료를 보존했습니다."
            ) from error

    def _recover_attachment_deletions(self) -> tuple[str, ...]:
        if not (self._paths.attachment_dir / ".trash").exists():
            return ()
        with self._coordinator.gate:
            storage = ManagedAttachmentStorage(
                self._paths.attachment_dir, coordinator=self._coordinator
            )
            if not self._paths.database_file.is_file():
                return ("삭제 복구를 위한 DB가 없어 격리 파일을 보존했습니다.",)
            with closing(
                sqlite3.connect(f"file:{self._paths.database_file.as_posix()}?mode=ro", uri=True)
            ) as connection:
                normalized = bool(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='file_assets'"
                    ).fetchone()
                )
                if normalized:
                    _check_asset_references(connection)
                table = "file_assets" if normalized else "attachments"

                def tracked(relative: str) -> bool:
                    state = " AND state='active'" if normalized else ""
                    return (
                        connection.execute(
                            f"SELECT 1 FROM {table} WHERE relative_path=?{state} LIMIT 1",
                            (relative,),
                        ).fetchone()
                        is not None
                    )

                def paths_for_name(name: str) -> tuple[str, ...]:
                    state = " AND state='active'" if normalized else ""
                    return tuple(
                        str(row[0])
                        for row in connection.execute(
                            f"SELECT relative_path FROM {table} WHERE stored_name=?{state} LIMIT 2",
                            (name,),
                        )
                    )

                return storage.recover_quarantine(tracked, paths_for_name=paths_for_name)

    @property
    def attachment_directory(self) -> Path:
        """Return the managed attachment directory for read-only user inspection."""
        return self._paths.attachment_dir

    @property
    def backup_directory(self) -> Path:
        """Return the directory containing verified OfficeFlow backup archives."""
        return self._paths.backup_dir

    def inspect_data_usage(
        self,
        *,
        now: datetime | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> DataUsageSnapshot:
        """Return a read-only inventory of records and files using local storage."""
        if not self._paths.database_file.is_file():
            raise BackupError("확인할 OfficeFlow 데이터베이스가 없습니다.")
        current = now or datetime.now(UTC)
        cutoff = (
            (current - timedelta(days=30))
            .astimezone(UTC)
            .replace(tzinfo=None)
            .strftime("%Y-%m-%d %H:%M:%S.%f")
        )
        reminder_cutoff = (
            (current - timedelta(days=180))
            .astimezone(UTC)
            .replace(tzinfo=None)
            .strftime("%Y-%m-%d %H:%M:%S.%f")
        )
        try:
            with closing(sqlite3.connect(self._paths.database_file)) as connection:
                connection.execute("PRAGMA busy_timeout=5000")
                connection.set_progress_handler(
                    lambda: int(cancel_requested is not None and cancel_requested()), 1_000
                )
                task_row = connection.execute(
                    """
                    SELECT
                        COUNT(*),
                        COALESCE(SUM(deleted_at IS NULL AND status IN ('active','pending')), 0),
                        COALESCE(SUM(deleted_at IS NULL AND status = 'completed'), 0),
                        COALESCE(SUM(deleted_at IS NULL AND status = 'archived'), 0),
                        COALESCE(SUM(deleted_at IS NOT NULL), 0),
                        COALESCE(SUM(deleted_at IS NOT NULL AND deleted_at <= ?), 0)
                    FROM tasks
                    """,
                    (cutoff,),
                ).fetchone()
                attachment_rows = connection.execute(
                    """
                    SELECT a.display_name, f.relative_path, COALESCE(f.observed_size_bytes, a.size_bytes), f.observed_checksum, a.detached_at, f.observed_mtime_ns, f.last_check_status
                    FROM attachments a JOIN file_assets f ON f.id=a.asset_id
                    """
                ).fetchall()
                asset_paths = {
                    str(row[0])
                    for row in connection.execute("SELECT relative_path FROM file_assets")
                }
                retained_counts = {}
                for table in ("task_history", "task_snapshots", "snapshot_files"):
                    if connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone():
                        retained_counts[table] = int(
                            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        )
                reminder_cleanup_candidate_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM reminder_deliveries
                        WHERE status IN ('acknowledged','completed','deferred')
                          AND acknowledged_at <= ?
                        """,
                        (reminder_cutoff,),
                    ).fetchone()[0]
                )
        except sqlite3.Error as error:
            _raise_if_canceled(cancel_requested)
            raise BackupError(f"데이터 사용량을 확인하지 못했습니다: {error}") from error
        if task_row is None:
            raise BackupError("업무 사용량을 확인하지 못했습니다.")

        tracked = {
            str(relative_path): (str(original_name), int(size_bytes), checksum, detached_at)
            for original_name, relative_path, size_bytes, checksum, detached_at, _mtime, _state in attachment_rows
        }
        linked = {
            relative: (name, size, checksum)
            for relative, (name, size, checksum, detached_at) in tracked.items()
            if detached_at is None
        }
        detached = {
            relative: (name, size, checksum, detached_at)
            for relative, (name, size, checksum, detached_at) in tracked.items()
            if detached_at is not None
        }
        checksum_sizes: dict[str, list[int]] = {}
        unverified_count = 0
        stored: dict[str, tuple[Path, int]] = {}
        quarantined: list[int] = []
        if self._paths.attachment_dir.is_dir():
            for path in self._paths.attachment_dir.rglob("*"):
                _raise_if_canceled(cancel_requested)
                relative_path = path.relative_to(self._paths.attachment_dir)
                if path.is_file() and relative_path.parts[0] == ".trash":
                    quarantined.append(path.stat().st_size)
                    continue
                if (
                    relative_path.parts[0] == "cache"
                    or not path.is_file()
                    or any(part.startswith(".") for part in relative_path.parts)
                ):
                    continue
                stored[relative_path.as_posix()] = (path, path.stat().st_size)

        for _name, relative, size, checksum, detached_at, mtime, state in attachment_rows:
            if detached_at is not None:
                continue
            sample = stored.get(str(relative))
            if (
                sample
                and checksum
                and state in {"unchanged", "modified", "no_baseline"}
                and sample[1] == size
                and sample[0].stat().st_mtime_ns == mtime
            ):
                checksum_sizes.setdefault(str(checksum), []).append(int(size))
            else:
                unverified_count += 1
        duplicate_groups = [sizes for sizes in checksum_sizes.values() if len(sizes) > 1]

        linked_paths = set(linked)
        tracked_paths = set(tracked)
        stored_paths = set(stored)
        coordinator = self._coordinator
        orphan_paths = {
            path
            for path in stored_paths - asset_paths
            if not coordinator.is_pinned(path) and not path.startswith("frozen/")
        }
        largest = sorted(
            (
                LargeStoredFile(
                    name=(
                        tracked[relative][0] if relative in tracked else stored[relative][0].name
                    ),
                    size_bytes=stored[relative][1],
                    orphaned=relative in orphan_paths,
                    detached=relative in detached,
                )
                for relative in stored_paths
            ),
            key=lambda item: item.size_bytes,
            reverse=True,
        )[:5]

        backups = tuple(self._paths.backup_dir.glob("*.ofbackup"))
        backup_sizes: list[int] = []
        automatic_backups: list[Path] = []
        manual_backups: list[Path] = []
        for path in backups:
            _raise_if_canceled(cancel_requested)
            if path.is_file():
                backup_sizes.append(path.stat().st_size)
                if path.name.startswith("officeflow-automatic-"):
                    automatic_backups.append(path)
                elif path.name.startswith("officeflow-manual-"):
                    manual_backups.append(path)

        latest_automatic_backup_at = (
            datetime.fromtimestamp(
                max(path.stat().st_mtime for path in automatic_backups),
                tz=UTC,
            )
            if automatic_backups
            else None
        )

        database_bytes = sum(
            path.stat().st_size
            for path in (
                self._paths.database_file,
                self._paths.database_file.with_name(self._paths.database_file.name + "-wal"),
                self._paths.database_file.with_name(self._paths.database_file.name + "-shm"),
            )
            if path.is_file()
        )
        return DataUsageSnapshot(
            task_count=int(task_row[0]),
            open_task_count=int(task_row[1]),
            completed_task_count=int(task_row[2]),
            archived_task_count=int(task_row[3]),
            trash_task_count=int(task_row[4]),
            aged_trash_task_count=int(task_row[5]),
            reminder_cleanup_candidate_count=reminder_cleanup_candidate_count,
            linked_attachment_count=len(linked),
            linked_attachment_bytes=sum(stored[path][1] for path in linked if path in stored),
            detached_attachment_count=len(detached),
            detached_attachment_bytes=sum(stored[path][1] for path in detached if path in stored),
            aged_detached_attachment_count=sum(
                1
                for _name, _size, _checksum, detached_at in detached.values()
                if detached_at is not None and str(detached_at) <= cutoff
            ),
            duplicate_attachment_count=sum(len(sizes) - 1 for sizes in duplicate_groups),
            duplicate_attachment_bytes=sum(sum(sorted(sizes)[1:]) for sizes in duplicate_groups),
            stored_attachment_count=len(stored),
            stored_attachment_bytes=sum(size for _path, size in stored.values()),
            missing_attachment_count=len(linked_paths - stored_paths),
            orphan_attachment_count=len(orphan_paths),
            orphan_attachment_bytes=sum(stored[path][1] for path in orphan_paths),
            backup_count=len(backup_sizes),
            backup_bytes=sum(backup_sizes),
            manual_backup_count=len(manual_backups),
            automatic_backup_count=len(automatic_backups),
            latest_automatic_backup_at=latest_automatic_backup_at,
            database_bytes=database_bytes,
            largest_files=tuple(largest),
            quarantine_file_count=len(quarantined),
            quarantine_bytes=sum(quarantined),
            unverified_attachment_count=unverified_count,
            unavailable_attachment_count=len(tracked_paths - stored_paths),
            history_count=retained_counts.get("task_history", 0),
            snapshot_count=retained_counts.get("task_snapshots", 0),
            snapshot_file_count=retained_counts.get("snapshot_files", 0),
        )

    def list_orphan_attachment_files(
        self,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> tuple[OrphanAttachmentFile, ...]:
        """Return stored files that have no attachment metadata and cannot be restored."""
        tracked = self._tracked_attachment_paths()
        items: list[OrphanAttachmentFile] = []
        if not self._paths.attachment_dir.is_dir():
            return ()
        for path in self._paths.attachment_dir.rglob("*"):
            _raise_if_canceled(cancel_requested)
            relative = path.relative_to(self._paths.attachment_dir)
            if not path.is_file() or any(part.startswith(".") for part in relative.parts):
                continue
            relative_path = relative.as_posix()
            if (
                relative_path in tracked
                or relative.parts[0] in {"frozen", "cache"}
                or self._coordinator.is_pinned(relative_path)
                or self._coordinator.is_protected(relative_path)
            ):
                continue
            stat = path.stat()
            items.append(
                OrphanAttachmentFile(
                    relative_path=relative_path,
                    name=path.name,
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                )
            )
        return tuple(sorted(items, key=lambda item: item.modified_at, reverse=True))

    def delete_orphan_attachment_file(self, relative_path: str) -> None:
        with self._coordinator.gate:
            self._coordinator.assert_not_pinned((relative_path,))
            self._delete_orphan_attachment_file_locked(relative_path)

    def _delete_orphan_attachment_file_locked(self, relative_path: str) -> None:
        """Delete one still-untracked file after the UI has obtained confirmation."""
        safe_relative = _safe_attachment_relative_path(relative_path)
        if safe_relative.parts[0] in {"frozen", "cache"} or any(
            part.startswith(".") for part in safe_relative.parts
        ):
            raise BackupError("격리·작업 파일은 일반 고아 파일 정리에서 삭제할 수 없습니다.")
        if safe_relative.as_posix() in self._tracked_attachment_paths():
            raise BackupError("이 파일은 현재 업무 또는 정리 대기 기록에 연결되어 있습니다.")
        root = self._paths.attachment_dir.resolve()
        target = (root / safe_relative).resolve()
        if not target.is_relative_to(root):
            raise BackupError("첨부파일 경로가 관리 폴더를 벗어납니다.")
        try:
            target.unlink()
        except FileNotFoundError as error:
            raise BackupError("삭제할 파일을 찾을 수 없습니다.") from error
        parent = target.parent
        while parent != root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def _tracked_attachment_paths(
        self, cancel_requested: Callable[[], bool] | None = None
    ) -> set[str]:
        if not self._paths.database_file.is_file():
            return set()
        try:
            return _database_attachment_paths(self._paths.database_file, cancel_requested)
        except sqlite3.Error as error:
            _raise_if_canceled(cancel_requested)
            raise BackupError(f"첨부파일 연결 정보를 확인하지 못했습니다: {error}") from error

    def create_backup(
        self,
        *,
        reason: str = "manual",
        keep: int | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> BackupInfo:
        from officeflow.infrastructure.backup_health import backup_error_summary

        started = False
        try:
            with self._coordinator.job(cancel_requested):
                self.health.begin(reason)
                started = True
                try:
                    result = self._create_backup(
                        reason=reason, keep=keep, cancel_requested=cancel_requested
                    )
                except (BackupCanceledError, FileJobCanceledError):
                    self.health.finish(reason, "canceled")
                    raise
                except Exception as error:
                    self.health.finish(reason, "failed", backup_error_summary(error))
                    raise
                else:
                    self.health.finish(reason, "success", result.maintenance_warning)
                    return result
        except FileJobCanceledError as error:
            if not started:
                self.health.waiting_canceled(reason)
            raise BackupCanceledError(str(error)) from error

    def _create_backup(
        self,
        *,
        reason: str = "manual",
        keep: int | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> BackupInfo:
        if not self._paths.database_file.is_file():
            raise BackupError("백업할 OfficeFlow 데이터베이스가 없습니다.")
        self._paths.ensure_directories()
        estimated_source_bytes = self._estimated_backup_source_bytes(cancel_requested)
        free_bytes = shutil.disk_usage(self._paths.root).free
        required_bytes = estimated_source_bytes * 2 + 16 * 1024 * 1024
        if free_bytes < required_bytes:
            raise BackupError(
                "백업 작업 공간이 부족합니다. "
                f"약 {_format_bytes(required_bytes)}가 필요하지만 "
                f"{_format_bytes(free_bytes)}만 사용할 수 있습니다."
            )
        created_at = datetime.now(UTC)
        stamp = created_at.astimezone().strftime("%Y%m%d-%H%M%S")
        safe_reason = (
            reason if reason in {"manual", "automatic", "pre-restore", "pre-import"} else "manual"
        )
        target = self._paths.backup_dir / (
            f"officeflow-{safe_reason}-{stamp}-{uuid.uuid4().hex[:8]}.ofbackup"
        )
        temporary_archive = target.with_suffix(".part")
        try:
            with tempfile.TemporaryDirectory(
                prefix=".backup-work-", dir=self._paths.root
            ) as temporary_name:
                working = Path(temporary_name)
                snapshot = working / "officeflow.db"
                captures = working / "captures"
                captures.mkdir()
                with ExitStack() as reservations:
                    with self._coordinator.gate:
                        self._snapshot_database(snapshot, cancel_requested)
                        referenced = _database_attachment_paths(snapshot, cancel_requested)
                        for relative in referenced:
                            reservations.enter_context(self._coordinator.preservation_pin(relative))
                        settings_source = working / "settings.json"
                        if self._paths.settings_file.is_file():
                            shutil.copy2(self._paths.settings_file, settings_source)
                    quarantined = self._quarantined_backup_sources(snapshot, cancel_requested)
                    missing = tuple(
                        sorted(
                            relative
                            for relative in referenced
                            if not self._managed_path(relative).is_file()
                            and relative not in quarantined
                        )
                    )
                    if missing:
                        raise BackupSourceIncompleteError(
                            "필요한 첨부파일이 누락되어 전체 백업하지 않았습니다: "
                            + ", ".join(missing[:5])
                        )
                    captured = self._capture_attachment_sources(captures, cancel_requested)
                    for relative, source in quarantined.items():
                        if relative not in captured and relative in referenced:
                            target_copy = captures / PurePosixPath(relative)
                            target_copy.parent.mkdir(parents=True, exist_ok=True)
                            _stable_copy(source, target_copy, cancel_requested)
                            captured[relative] = target_copy, _file_identity(target_copy)
                    if referenced - set(captured):
                        raise BackupError(
                            "백업 도중 첨부파일이 변경되거나 사라졌습니다. 다시 백업해 주세요."
                        )
                staged = working / "attachments"
                attachments = self._stage_attachment_sources(captured, staged, cancel_requested)
                assets = _database_assets(snapshot, cancel_requested)
                attachments = tuple(
                    replace(item, category="asset", asset_id=assets[item.path][0])
                    if item.path in assets
                    else replace(item, category="asset")
                    if item.path in referenced
                    else item
                    for item in attachments
                )
                _verify_asset_captures(assets, attachments)
                table_counts = _database_counts(snapshot, cancel_requested)
                settings = (
                    BackupFile(
                        path="settings.json",
                        size=settings_source.stat().st_size,
                        sha256=_sha256(settings_source, cancel_requested),
                    )
                    if settings_source.is_file()
                    else None
                )
                manifest = BackupManifest(
                    format_version=_FORMAT_VERSION,
                    created_at=created_at.isoformat(),
                    reason=safe_reason,
                    database_sha256=_sha256(snapshot, cancel_requested),
                    table_counts=table_counts,
                    attachments=attachments,
                    settings=settings,
                    missing_attachments=missing,
                    recovery_partition=True,
                )
                with zipfile.ZipFile(
                    temporary_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
                ) as archive:
                    _write_archive_file(archive, snapshot, "data/officeflow.db", cancel_requested)
                    for item in attachments:
                        _write_archive_file(
                            archive,
                            staged / PurePosixPath(item.path),
                            _backup_member(item, manifest),
                            cancel_requested,
                        )
                    if settings is not None:
                        _write_archive_file(
                            archive,
                            settings_source,
                            "settings.json",
                            cancel_requested,
                        )
                    archive.writestr(
                        "manifest.json",
                        json.dumps(
                            _manifest_payload(manifest), ensure_ascii=False, indent=2
                        ).encode("utf-8"),
                    )
            _raise_if_canceled(cancel_requested)
            self.verify_backup(temporary_archive, cancel_requested=cancel_requested)
            os.replace(temporary_archive, target)
        except (OSError, sqlite3.Error, zipfile.BadZipFile, ValueError, FileInUseError) as error:
            _raise_if_canceled(cancel_requested)
            raise BackupError(f"백업을 만들지 못했습니다: {error}") from error
        finally:
            temporary_archive.unlink(missing_ok=True)
        maintenance_warning = ""
        if safe_reason == "automatic" and keep is not None:
            try:
                self.prune_automatic_backups(keep)
            except OSError:
                # Publication/verification succeeded; retention is a separate outcome.
                maintenance_warning = "새 백업은 성공했지만 오래된 자동 백업을 정리하지 못했습니다. 백업 폴더 접근 상태를 확인하세요."
                logger.warning("Automatic backup retention failed after successful publication")
        return BackupInfo(
            path=target,
            created_at=created_at,
            reason=safe_reason,
            table_counts=table_counts,
            attachment_count=len(attachments),
            missing_attachments=missing,
            maintenance_warning=maintenance_warning,
        )

    def _estimated_backup_source_bytes(
        self, cancel_requested: Callable[[], bool] | None = None
    ) -> int:
        _raise_if_canceled(cancel_requested)
        paths = (self._paths.database_file, self._paths.settings_file)
        total = sum(path.stat().st_size for path in paths if path.is_file())
        if self._paths.attachment_dir.is_dir():
            for path in self._paths.attachment_dir.rglob("*"):
                _raise_if_canceled(cancel_requested)
                parts = path.relative_to(self._paths.attachment_dir).parts
                if path.is_file() and parts[0] != "cache":
                    total += path.stat().st_size
        return total

    def verify_backup(
        self,
        archive_path: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
        check_schema: bool = False,
    ) -> BackupManifest:
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                names = set(archive.namelist())
                if len(names) != len(archive.infolist()):
                    raise BackupError("백업에 중복된 파일 경로가 있습니다.")
                for name in names:
                    _raise_if_canceled(cancel_requested)
                    _validate_archive_name(name)
                if "manifest.json" not in names or "data/officeflow.db" not in names:
                    raise BackupError("백업에 필수 파일이 없습니다.")
                manifest = _parse_manifest(archive.read("manifest.json"))
                expected = {"manifest.json", "data/officeflow.db"}
                expected.update(_backup_member(item, manifest) for item in manifest.attachments)
                if manifest.settings is not None:
                    expected.add("settings.json")
                if names != expected:
                    raise BackupError("백업 목록과 실제 파일 구성이 일치하지 않습니다.")
                if (
                    _stream_digest(archive.open("data/officeflow.db"), cancel_requested)
                    != manifest.database_sha256
                ):
                    raise BackupError("백업 데이터베이스 체크섬이 일치하지 않습니다.")
                for item in manifest.attachments:
                    member = _backup_member(item, manifest)
                    info = archive.getinfo(member)
                    if info.file_size != item.size:
                        raise BackupError(f"첨부파일 크기가 일치하지 않습니다: {item.path}")
                    if _stream_digest(archive.open(member), cancel_requested) != item.sha256:
                        raise BackupError(f"첨부파일 체크섬이 일치하지 않습니다: {item.path}")
                if manifest.settings is not None:
                    info = archive.getinfo("settings.json")
                    if info.file_size != manifest.settings.size:
                        raise BackupError("설정 파일 크기가 일치하지 않습니다.")
                    if (
                        _stream_digest(archive.open("settings.json"), cancel_requested)
                        != manifest.settings.sha256
                    ):
                        raise BackupError("설정 파일 체크섬이 일치하지 않습니다.")
                with tempfile.TemporaryDirectory(
                    prefix=".backup-verify-", dir=self._paths.root
                ) as temporary_name:
                    database = Path(temporary_name) / "officeflow.db"
                    with archive.open("data/officeflow.db") as source, database.open("wb") as sink:
                        _copy_stream(source, sink, cancel_requested)
                    if _database_counts(database, cancel_requested) != manifest.table_counts:
                        raise BackupError("백업 데이터베이스의 레코드 개수가 일치하지 않습니다.")
                    if check_schema:
                        _check_restore_schema(database, cancel_requested)
                    referenced = _database_attachment_paths(database, cancel_requested)
                    absent = referenced - {item.path for item in manifest.attachments}
                    if manifest.format_version == 1:
                        # Old backups did not declare missing files. Preserve compatibility
                        # but expose their loss instead of silently calling them complete.
                        manifest = replace(manifest, missing_attachments=tuple(sorted(absent)))
                    elif absent != set(manifest.missing_attachments):
                        raise BackupError(
                            "백업 DB의 첨부 연결과 누락 파일 명세가 일치하지 않습니다."
                        )
                    if manifest.format_version == 3:
                        if manifest.missing_attachments:
                            raise BackupError(
                                "새 전체 백업 형식에는 필요한 파일 누락을 허용하지 않습니다."
                            )
                        _check_restore_schema(database, cancel_requested)
                        _verify_asset_captures(
                            _database_assets(database, cancel_requested), manifest.attachments
                        )
                    else:
                        _verify_asset_captures(
                            _database_assets(database, cancel_requested),
                            manifest.attachments,
                            strict=False,
                        )
                return manifest
        except BackupError:
            raise
        except (
            OSError,
            KeyError,
            json.JSONDecodeError,
            zipfile.BadZipFile,
            sqlite3.Error,
        ) as error:
            _raise_if_canceled(cancel_requested)
            raise BackupError(f"백업 검증에 실패했습니다: {error}") from error

    def stage_restore(
        self,
        archive_path: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> BackupManifest:
        source = archive_path.resolve()
        if source == self._paths.pending_restore_file.resolve():
            return self.verify_backup(source, cancel_requested=cancel_requested, check_schema=True)
        manifest = self.verify_backup(source, cancel_requested=cancel_requested, check_schema=True)
        pending = self._paths.pending_restore_file
        temporary = pending.with_suffix(".part")
        try:
            with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
                _copy_stream(input_stream, output_stream, cancel_requested)
            self.verify_backup(temporary, cancel_requested=cancel_requested, check_schema=True)
            os.replace(temporary, pending)
        finally:
            temporary.unlink(missing_ok=True)
        return manifest

    def cancel_pending_restore(self) -> Path | None:
        """Disarm a failed reservation without destroying its recoverable archive."""
        if self._restore_state_file.exists():
            raise BackupError("중단된 복원의 복구가 끝나기 전에는 예약을 취소할 수 없습니다.")
        pending = self._paths.pending_restore_file
        if not pending.is_file():
            return None
        self._paths.backup_dir.mkdir(parents=True, exist_ok=True)
        retained = self._paths.backup_dir / f"canceled-restore-{uuid.uuid4().hex}.ofbackup"
        pending.replace(retained)
        return retained

    def apply_pending_restore(self) -> BackupInfo | None:
        self.restore_warnings = ()
        if self._recover_restore_transaction():
            return None
        pending = self._paths.pending_restore_file
        if not pending.is_file():
            return None
        manifest = self.verify_backup(pending, check_schema=True)
        self._paths.ensure_directories()
        preserve_rollback = False
        try:
            pre_restore = self.create_backup(reason="pre-restore")
        except BackupSourceIncompleteError:
            # Missing/altered bytes are often the reason for restoring. Preserve
            # the complete previous directory rather than inventing a full backup
            # or purging those remaining bytes after the successful swap.
            _check_restore_schema(self._paths.database_file)
            _database_counts(self._paths.database_file)
            pre_restore = None
            preserve_rollback = True
        token = uuid.uuid4().hex
        rollback = self._paths.root / f".restore-rollback-{token}"
        staging = self._paths.root / f".restore-stage-{token}"
        state: dict[str, Any] = {
            "phase": "swapping",
            "rollback": rollback.name,
            "staging": staging.name,
            "pending_sha256": _sha256(pending),
            "had_settings": self._paths.settings_file.is_file(),
            "old_database_sha256": _sha256(self._paths.database_file),
            "preserve_rollback": preserve_rollback,
        }
        staging.mkdir()
        try:
            self._extract_verified(pending, staging)
            rollback.mkdir()
            self._write_restore_state(state)
            self._swap_restored_data(staging, rollback, manifest)
            # Once this durable commit exists, old files are never rollback input.
            state["phase"] = "committed"
            self._write_restore_state(state)
        except Exception as error:
            try:
                self._rollback_restore(rollback, had_settings=bool(state["had_settings"]))
                if _sha256(self._paths.database_file) != state["old_database_sha256"]:
                    raise BackupError("되돌린 데이터베이스가 이전 원본과 일치하지 않습니다.")
                state["phase"] = "rolled_back"
                self._write_restore_state(state)
                self._cleanup_restore_transaction(state)
            except Exception as rollback_error:
                raise BackupError(
                    "복원 중단 자료를 자동 복구하지 못했습니다. 현재 DB를 열지 않고 복구 자료를 보존합니다."
                ) from rollback_error
            raise BackupError(f"복원을 적용하지 못했습니다: {error}") from error
        self._cleanup_restore_transaction(state)
        return pre_restore

    @property
    def _restore_state_file(self) -> Path:
        return self._paths.root / ".restore-state.json"

    @property
    def restore_is_pending(self) -> bool:
        return self._paths.pending_restore_file.is_file() or self._restore_state_file.exists()

    @property
    def restore_is_incomplete(self) -> bool:
        if not self._restore_state_file.is_file():
            return False
        try:
            return bool(self._read_restore_state()["phase"] == "swapping")
        except BackupError:
            return True

    def _write_restore_state(self, state: dict[str, Any]) -> None:
        temporary = self._restore_state_file.with_suffix(".part")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self._restore_state_file)

    def _read_restore_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self._restore_state_file.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("phase") not in (
                "swapping",
                "committed",
                "rolled_back",
            ):
                raise ValueError("invalid restore phase")
            for key, prefix in (("rollback", ".restore-rollback-"), ("staging", ".restore-stage-")):
                name = state[key]
                if (
                    not isinstance(name, str)
                    or not name.startswith(prefix)
                    or len(name.removeprefix(prefix)) != 32
                    or any(char not in "0123456789abcdef" for char in name.removeprefix(prefix))
                ):
                    raise ValueError("invalid restore folder")
                self._restore_work_directory(name)
            if not isinstance(state.get("had_settings"), bool) or any(
                not isinstance(state.get(key), str) or len(state[key]) != 64
                for key in ("pending_sha256", "old_database_sha256")
            ):
                raise ValueError("invalid restore metadata")
            if not isinstance(state.get("preserve_rollback", False), bool):
                raise ValueError("invalid preservation state")
            return state
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise BackupError(
                "복원 단계 기록을 읽지 못했습니다. 보호 자료를 보존합니다."
            ) from error

    def _restore_work_directory(self, name: str) -> Path:
        root = self._paths.root.resolve()
        target = root / name
        if target.resolve().parent != root or target.is_symlink():
            raise BackupError("복원 작업 폴더가 데이터 폴더를 벗어납니다.")
        return target

    def _recover_restore_transaction(self) -> bool:
        if not self._restore_state_file.is_file():
            return False
        state = self._read_restore_state()
        if state["phase"] == "swapping":
            try:
                self._rollback_restore(
                    self._restore_work_directory(state["rollback"]),
                    had_settings=state["had_settings"],
                )
                if _sha256(self._paths.database_file) != state["old_database_sha256"]:
                    raise BackupError("중단된 복원의 이전 DB를 확인하지 못했습니다.")
                state["phase"] = "rolled_back"
                self._write_restore_state(state)
            except Exception as error:
                raise BackupError(
                    "중단된 복원을 되돌리지 못했습니다. 복구 자료를 보존합니다."
                ) from error
        self._cleanup_restore_transaction(state)
        # An unresolved old cleanup must not be overwritten by a new transaction.
        if self._restore_state_file.exists() and state["phase"] == "rolled_back":
            raise BackupError("이전 복원 작업의 정리를 마친 뒤 다시 복원해 주세요.")
        return self._restore_state_file.exists()

    def _cleanup_restore_transaction(self, state: dict[str, Any]) -> None:
        if state["phase"] == "committed":
            old_cache = self._restore_work_directory(state["rollback"]) / "attachments" / "cache"
            if old_cache.exists():
                new_cache = self._paths.attachment_dir / "cache"
                if new_cache.exists():
                    raise BackupError(
                        "이전 보기 사본과 복원 영역이 충돌했습니다. 자료를 보존했습니다."
                    )
                os.replace(old_cache, new_cache)
            marker = self._paths.attachment_dir / "cache" / "attachment-views" / "generation.json"
            if marker.parent.is_dir():
                # Deterministic for this restore transaction, including a crash after
                # moving the cache but before publishing its generation marker.
                temporary = marker.with_name(f"generation-{uuid.uuid4().hex}.part")
                with temporary.open("x", encoding="utf-8") as stream:
                    generation = uuid.uuid5(
                        uuid.NAMESPACE_URL, "officeflow:restore-cache:" + state["rollback"]
                    ).hex
                    json.dump({"generation": generation}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, marker)
        try:
            if (
                state["phase"] == "committed"
                and self._paths.pending_restore_file.exists()
                and _sha256(self._paths.pending_restore_file) == state["pending_sha256"]
            ):
                self._paths.pending_restore_file.unlink()
            for key in ("staging", "rollback"):
                folder = self._restore_work_directory(state[key])
                if (
                    key == "rollback"
                    and state["phase"] == "committed"
                    and state.get("preserve_rollback", False)
                ):
                    target = self._paths.backup_dir / (
                        "pre-restore-preserved-"
                        + state["rollback"].removeprefix(".restore-rollback-")
                    )
                    if folder.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.exists():
                            raise FileExistsError(
                                "이전 자료 보존 위치가 이미 있습니다. 덮어쓰지 않았습니다."
                            )
                        folder.rename(target)
                    self.restore_warnings += (
                        f"복원 전 자료는 파일 누락/고정 불일치로 전체 백업하지 못해 별도로 보존했습니다: {target} (정상 전체 백업 아님)",
                    )
                    continue
                if folder.exists():
                    shutil.rmtree(folder)
            self._restore_state_file.unlink(missing_ok=True)
        except OSError as error:
            message = f"복원 결과는 유지했습니다. 이전 복원 자료 정리는 다음 실행 때 재시도합니다: {error}"
            self.restore_warnings += (message,)
            logger.warning(message)

    def should_create_automatic_backup(self, interval_hours: int) -> bool:
        latest = max(
            self._paths.backup_dir.glob("officeflow-automatic-*.ofbackup"),
            key=lambda path: path.stat().st_mtime,
            default=None,
        )
        if latest is None:
            return True
        modified = datetime.fromtimestamp(latest.stat().st_mtime, tz=UTC)
        return datetime.now(UTC) - modified >= timedelta(hours=interval_hours)

    def prune_automatic_backups(self, keep: int) -> tuple[Path, ...]:
        if keep < 1:
            raise ValueError("자동 백업은 한 개 이상 보관해야 합니다.")
        backups = sorted(
            self._paths.backup_dir.glob("officeflow-automatic-*.ofbackup"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        removed: list[Path] = []
        for path in backups[keep:]:
            path.unlink()
            removed.append(path)
        return tuple(removed)

    def list_manual_backups(self) -> tuple[Path, ...]:
        """Return manual backups from newest to oldest."""
        return tuple(
            sorted(
                self._paths.backup_dir.glob("officeflow-manual-*.ofbackup"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        )

    def prune_manual_backups(self, keep: int) -> tuple[Path, ...]:
        """Remove old manual backups while retaining the newest requested count."""
        if keep < 1:
            raise ValueError("수동 백업은 한 개 이상 보관해야 합니다.")
        removed: list[Path] = []
        for path in self.list_manual_backups()[keep:]:
            path.unlink()
            removed.append(path)
        return tuple(removed)

    def _snapshot_database(
        self,
        destination: Path,
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        with (
            closing(sqlite3.connect(self._paths.database_file)) as source,
            closing(sqlite3.connect(destination)) as target,
        ):
            source.backup(
                target,
                pages=256,
                progress=lambda _status, _remaining, _total: _raise_if_canceled(cancel_requested),
            )

    def _managed_path(self, relative: str) -> Path:
        safe = _safe_attachment_relative_path(relative)
        root = self._paths.attachment_dir.resolve()
        path = (root / safe).resolve()
        if not path.is_relative_to(root):
            raise BackupError("첨부파일 경로가 관리 폴더를 벗어납니다.")
        return path

    def _capture_attachment_sources(
        self, destination: Path, canceled: Callable[[], bool] | None
    ) -> dict[str, tuple[Path, tuple[int, int, int, int]]]:
        """Capture independent bytes outside the publication/deletion gate."""
        captured: dict[str, tuple[Path, tuple[int, int, int, int]]] = {}
        for source in self._paths.attachment_dir.rglob("*"):
            _raise_if_canceled(canceled)
            relative = source.relative_to(self._paths.attachment_dir)
            if relative.parts[0] == "cache" or not source.is_file():
                continue
            source = checked_managed_path(
                self._paths.attachment_dir, relative.as_posix(), file_required=True
            )
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with self._coordinator.preservation_pin(relative.as_posix()):
                _stable_copy(source, target, canceled)
            captured[relative.as_posix()] = (target, _file_identity(target))
        return captured

    def _quarantined_backup_sources(
        self, database: Path, canceled: Callable[[], bool] | None
    ) -> dict[str, Path]:
        """Only an exact owned delete journal can supply a missing live asset.

        Recovery bytes are additionally backed up as evidence; restore never replays it.
        """
        storage = DeletionFileStorage(self._paths.attachment_dir, coordinator=self._coordinator)
        represented: set[str] = set()
        sources: dict[str, Path] = {}
        folder = self._paths.attachment_dir / ".trash"
        if folder.exists():
            with os.scandir(folder) as jobs:
                for job in jobs:
                    _raise_if_canceled(canceled)
                    if not job.name.startswith("delete-"):
                        continue
                    try:
                        command, entries = storage._read(job.name.removeprefix("delete-"))
                        for entry in entries:
                            represented.add(entry.asset.id)
                            path = checked_managed_path(
                                self._paths.attachment_dir,
                                f".trash/delete-{command.job_id}/{entry.asset.id}",
                            )
                            if (
                                entry.state == "prepared"
                                and path.is_file()
                                and _file_identity(path) == entry.identity
                            ):
                                if entry.asset.path in sources:
                                    raise BackupError(
                                        "같은 자산의 격리 자료가 중복되어 백업하지 않았습니다."
                                    )
                                sources[entry.asset.path] = path
                    except (ValueError, OSError, AttachmentOperationError):
                        # Unknown materials are preserved, but cannot supply a required asset.
                        continue
        with closing(sqlite3.connect(database)) as connection:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='file_assets'"
            ).fetchone():
                for (asset_id,) in connection.execute(
                    "SELECT id FROM file_assets WHERE state='pending_delete'"
                ):
                    if asset_id not in represented:
                        raise BackupError(
                            "삭제 후처리 자산의 저널이 없어 전체 백업하지 않았습니다. 자료 점검이 필요합니다."
                        )
        return sources

    def _stage_attachment_sources(
        self,
        sources: dict[str, tuple[Path, tuple[int, int, int, int]]],
        destination: Path,
        canceled: Callable[[], bool] | None,
    ) -> tuple[BackupFile, ...]:
        items: list[BackupFile] = []
        for relative, (source, identity) in sources.items():
            _raise_if_canceled(canceled)
            if _file_identity(source) != identity:
                raise BackupError(f"백업 도중 첨부파일 내용이 변경되었습니다: {relative}")
            target = destination / PurePosixPath(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            _stable_copy(source, target, canceled)
            items.append(BackupFile(relative, target.stat().st_size, _sha256(target, canceled)))
        return tuple(items)

    def _extract_verified(self, archive_path: Path, destination: Path) -> None:
        with zipfile.ZipFile(archive_path, "r") as archive:
            for member in archive.infolist():
                _validate_archive_name(member.filename)
                if member.filename == "manifest.json":
                    continue
                target = destination / PurePosixPath(member.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink, length=1024 * 1024)

    def _swap_restored_data(self, staging: Path, rollback: Path, manifest: BackupManifest) -> None:
        old_data = rollback / "data"
        old_data.mkdir()
        for path in (
            self._paths.database_file,
            self._paths.database_file.with_name(self._paths.database_file.name + "-wal"),
            self._paths.database_file.with_name(self._paths.database_file.name + "-shm"),
        ):
            if path.exists():
                os.replace(path, old_data / path.name)
        if self._paths.attachment_dir.exists():
            os.replace(self._paths.attachment_dir, rollback / "attachments")
        staged_settings = staging / "settings.json"
        if self._paths.settings_file.exists() and staged_settings.exists():
            os.replace(self._paths.settings_file, rollback / "settings.json")

        self._paths.data_dir.mkdir(parents=True, exist_ok=True)
        os.replace(staging / "data" / "officeflow.db", self._paths.database_file)
        staged_attachments = staging / "attachments"
        staged_attachments.mkdir(exist_ok=True)
        os.replace(staged_attachments, self._paths.attachment_dir)
        if staged_settings.exists():
            os.replace(staged_settings, self._paths.settings_file)

        if _database_counts(self._paths.database_file) != manifest.table_counts:
            raise BackupError("복원된 데이터베이스의 레코드 개수가 일치하지 않습니다.")
        for item in manifest.attachments:
            restored = (
                staging / "recovery"
                if manifest.recovery_partition and item.category == "recovery"
                else self._paths.attachment_dir
            ) / PurePosixPath(item.path)
            if not restored.is_file() or _sha256(restored) != item.sha256:
                raise BackupError(f"복원된 첨부파일 검증에 실패했습니다: {item.path}")
        if (staging / "recovery").exists():
            recovery = self._paths.root / "recovery" / rollback.name
            recovery.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / "recovery", recovery)
            self.restore_warnings += (f"중단/미분류 자료는 자동 등록하지 않았습니다: {recovery}",)

    def _rollback_restore(self, rollback: Path, *, had_settings: bool = True) -> None:
        if not rollback.exists():
            return
        old_data = rollback / "data"
        old_database = old_data / self._paths.database_file.name
        if old_database.exists():
            for name in (
                self._paths.database_file.name,
                self._paths.database_file.name + "-wal",
                self._paths.database_file.name + "-shm",
            ):
                (self._paths.data_dir / name).unlink(missing_ok=True)
            for path in old_data.iterdir():
                os.replace(path, self._paths.data_dir / path.name)
        old_attachments = rollback / "attachments"
        if old_attachments.exists():
            if self._paths.attachment_dir.exists():
                shutil.rmtree(self._paths.attachment_dir)
            os.replace(old_attachments, self._paths.attachment_dir)
        old_settings = rollback / "settings.json"
        if old_settings.exists():
            self._paths.settings_file.unlink(missing_ok=True)
            os.replace(old_settings, self._paths.settings_file)
        elif not had_settings:
            self._paths.settings_file.unlink(missing_ok=True)


def _database_counts(
    path: Path, cancel_requested: Callable[[], bool] | None = None
) -> dict[str, int]:
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
            _raise_if_canceled(cancel_requested)
            connection.set_progress_handler(
                lambda: int(cancel_requested is not None and cancel_requested()), 1_000
            )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise BackupError("SQLite 무결성 검사에 실패했습니다.")
            tables = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            counts: dict[str, int] = {}
            for (name,) in tables:
                _raise_if_canceled(cancel_requested)
                if not isinstance(name, str):
                    raise BackupError("데이터베이스 테이블 이름이 올바르지 않습니다.")
                quoted = name.replace('"', '""')
                row = connection.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()
                if row is None:
                    raise BackupError(f"테이블 개수를 확인하지 못했습니다: {name}")
                counts[name] = int(row[0])
            return counts
    except sqlite3.Error as error:
        _raise_if_canceled(cancel_requested)
        raise BackupError(f"SQLite 검증에 실패했습니다: {error}") from error


def _database_attachment_paths(
    path: Path, cancel_requested: Callable[[], bool] | None = None
) -> set[str]:
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
        connection.set_progress_handler(
            lambda: int(cancel_requested is not None and cancel_requested()), 1_000
        )
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'"
        ).fetchone():
            return set()
        paths: set[str] = set()
        normalized = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='file_assets' AND type='table'"
        ).fetchone()
        if normalized:
            _check_asset_references(connection)
        statement = (
            "SELECT relative_path FROM file_assets WHERE state='active'"
            if normalized
            else "SELECT relative_path FROM attachments"
        )
        for row in connection.execute(statement):
            _raise_if_canceled(cancel_requested)
            paths.add(str(row[0]))
        return paths


def _check_restore_schema(path: Path, cancel_requested: Callable[[], bool] | None = None) -> None:
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
        _raise_if_canceled(cancel_requested)
        connection.set_progress_handler(
            lambda: int(cancel_requested is not None and cancel_requested()), 1_000
        )
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"tasks", "attachments", "work_logs", "alembic_version"} <= tables:
            raise BackupError("OfficeFlow v3 데이터베이스 구조가 아닌 백업은 복원할 수 없습니다.")
        for table, required in (
            ("tasks", {"id", "title", "status", "created_at"}),
            ("attachments", {"id", "task_id"}),
            ("work_logs", {"id", "log_date", "content"}),
        ):
            _raise_if_canceled(cancel_requested)
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if not required <= columns:
                raise BackupError(f"백업 DB의 필수 열이 누락되었습니다: {table}")
            if table == "attachments" and not (
                {"relative_path"} <= columns
                or (
                    {"asset_id", "family_id", "display_name"} <= columns
                    and {"file_assets", "document_families", "attachment_versions"} <= tables
                )
            ):
                raise BackupError("백업 DB의 첨부 파일 원장이 누락되었습니다.")
        revisions = {
            row[0] for row in connection.execute("SELECT version_num FROM alembic_version")
        }
        known = {
            revision.revision
            for revision in ScriptDirectory.from_config(migration_config(path)).walk_revisions()
        }
        if len(revisions) != 1 or not revisions <= known:
            raise BackupError(
                "이 프로그램이 지원하지 않는 DB 버전입니다. 더 새로운 OfficeFlow에서 복원하세요."
            )
        if revisions <= {"0016_task_folder_links", "0017_completion_browse_index"}:
            required_links = {"id", "task_id", "name", "path", "position", "created_at"}
            columns = {
                row[1] for row in connection.execute('PRAGMA table_info("task_folder_links")')
            }
            if "task_folder_links" not in tables or not required_links <= columns:
                raise BackupError("백업 DB의 외부 폴더 연결 정보가 누락되었습니다.")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("백업 데이터베이스의 업무 연결 정보가 손상되었습니다.")
        if "file_assets" in tables:
            _check_asset_references(connection)


def _check_asset_references(connection: sqlite3.Connection) -> None:
    try:
        assert_supported_sqlite_asset_references(connection)
    except ValueError as error:
        raise BackupError(str(error)) from error
    if (
        connection.execute("""SELECT 1 FROM attachments a
        JOIN file_assets f ON f.id=a.asset_id JOIN document_families d ON d.id=a.family_id
        WHERE f.state!='active' OR d.task_id!=a.task_id
        OR (a.role='original' AND f.kind!='frozen') OR (a.role IN ('general','work') AND f.kind!='mutable') LIMIT 1""").fetchone()
        or connection.execute("""SELECT 1 FROM attachment_versions v
        JOIN file_assets f ON f.id=v.asset_id JOIN document_families d ON d.id=v.family_id
        WHERE f.state!='active' OR f.kind!='frozen' OR d.task_id!=v.task_id LIMIT 1""").fetchone()
    ):
        raise BackupError(
            "첨부 자산의 종류·소유·상태가 일치하지 않습니다. 자료를 보존하고 확인해 주세요."
        )
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshot_files'"
    ).fetchone():
        if connection.execute("""SELECT 1 FROM snapshot_files s JOIN file_assets f ON f.id=s.asset_id
            WHERE f.state!='active' OR f.kind!='frozen' OR f.fixed_size_bytes!=s.fixed_size_bytes
            OR f.fixed_checksum!=s.fixed_checksum LIMIT 1""").fetchone():
            raise BackupError("중요 보관 파일의 연결/고정 기준이 일치하지 않습니다.")
        if connection.execute("""SELECT 1 FROM task_snapshots s WHERE s.file_count !=
            (SELECT COUNT(*) FROM snapshot_files f WHERE f.snapshot_id=s.id) LIMIT 1""").fetchone():
            raise BackupError("중요 보관의 파일 개수가 일치하지 않습니다.")


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _stable_copy(source: Path, target: Path, canceled: Callable[[], bool] | None) -> None:
    before = _file_identity(source)
    with protected_read(source) as input_stream, target.open("xb") as output_stream:
        _copy_stream(input_stream, output_stream, canceled)
        output_stream.flush()
        os.fsync(output_stream.fileno())
    if _file_identity(source) != before or target.stat().st_size != before[2]:
        raise BackupError(f"백업 사본 확보 중 파일이 변경되었습니다: {source.name}")


def _raise_if_canceled(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise BackupCanceledError("데이터 작업을 취소했습니다.")


def _sha256(path: Path, cancel_requested: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            _raise_if_canceled(cancel_requested)
            digest.update(chunk)
    return digest.hexdigest()


def _stream_digest(stream: Any, cancel_requested: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    try:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            _raise_if_canceled(cancel_requested)
            digest.update(chunk)
    finally:
        stream.close()
    return digest.hexdigest()


def _copy_stream(
    source: Any,
    destination: Any,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    while chunk := source.read(1024 * 1024):
        _raise_if_canceled(cancel_requested)
        destination.write(chunk)


def _write_archive_file(
    archive: zipfile.ZipFile,
    source_path: Path,
    member: str,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    with source_path.open("rb") as source, archive.open(member, "w") as destination:
        _copy_stream(source, destination, cancel_requested)


def _validate_archive_name(name: str) -> None:
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts:
        raise BackupError("백업에 안전하지 않은 파일 경로가 있습니다.")
    allowed = name in {"manifest.json", "data/officeflow.db", "settings.json"} or (
        len(path.parts) >= 2 and path.parts[0] in {"attachments", "recovery"}
    )
    if not allowed:
        raise BackupError(f"백업에 알 수 없는 파일이 있습니다: {name}")


def _manifest_payload(manifest: BackupManifest) -> dict[str, Any]:
    payload = asdict(manifest)
    payload["attachments"] = [asdict(item) for item in manifest.attachments]
    payload["settings"] = asdict(manifest.settings) if manifest.settings is not None else None
    return payload


def _backup_member(item: BackupFile, manifest: BackupManifest) -> str:
    prefix = (
        "recovery" if manifest.recovery_partition and item.category == "recovery" else "attachments"
    )
    return f"{prefix}/{item.path}"


def _parse_manifest(payload: bytes) -> BackupManifest:
    raw = json.loads(payload.decode("utf-8"))
    if not isinstance(raw, dict) or raw.get("format_version") not in (1, 2, _FORMAT_VERSION):
        raise BackupError("지원하지 않는 백업 형식입니다.")
    attachments_raw = raw.get("attachments")
    counts_raw = raw.get("table_counts")
    if not isinstance(attachments_raw, list) or not isinstance(counts_raw, dict):
        raise BackupError("백업 명세가 올바르지 않습니다.")
    attachments = tuple(_parse_file(item) for item in attachments_raw)
    if len({item.path for item in attachments}) != len(attachments):
        raise BackupError("백업 명세에 중복된 첨부파일이 있습니다.")
    settings_raw = raw.get("settings")
    missing_raw = raw.get("missing_attachments", [])
    if not isinstance(missing_raw, list) or not all(isinstance(item, str) for item in missing_raw):
        raise BackupError("백업 누락 파일 명세가 올바르지 않습니다.")
    for relative in missing_raw:
        _safe_attachment_relative_path(relative)
    if len(set(missing_raw)) != len(missing_raw):
        raise BackupError("백업 누락 파일 명세에 중복이 있습니다.")
    settings = _parse_file(settings_raw) if settings_raw is not None else None
    if settings is not None and settings.path != "settings.json":
        raise BackupError("백업 설정 파일 경로가 올바르지 않습니다.")
    try:
        if type(raw.get("recovery_partition", False)) is not bool:
            raise ValueError("복구 영역 명세가 올바르지 않습니다.")
        counts = {str(key): int(value) for key, value in counts_raw.items()}
        manifest = BackupManifest(
            format_version=int(raw["format_version"]),
            created_at=str(raw["created_at"]),
            reason=str(raw["reason"]),
            database_sha256=str(raw["database_sha256"]),
            table_counts=counts,
            attachments=attachments,
            settings=settings,
            missing_attachments=tuple(missing_raw),
            recovery_partition=raw.get("recovery_partition", False),
        )
        datetime.fromisoformat(manifest.created_at)
    except (KeyError, TypeError, ValueError) as error:
        raise BackupError("백업 명세 필드가 올바르지 않습니다.") from error
    if len(manifest.database_sha256) != 64:
        raise BackupError("백업 데이터베이스 체크섬이 올바르지 않습니다.")
    if any(value < 0 for value in manifest.table_counts.values()):
        raise BackupError("백업 레코드 개수가 올바르지 않습니다.")
    return manifest


def _parse_file(raw: Any) -> BackupFile:
    if not isinstance(raw, dict):
        raise BackupError("백업 파일 명세가 올바르지 않습니다.")
    try:
        item = BackupFile(
            path=str(raw["path"]),
            size=int(raw["size"]),
            sha256=str(raw["sha256"]),
            category=str(raw.get("category", "recovery")),
            asset_id=raw.get("asset_id"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise BackupError("백업 파일 명세 필드가 올바르지 않습니다.") from error
    _validate_archive_name(
        f"attachments/{item.path}" if item.path != "settings.json" else item.path
    )
    if item.size < 0 or len(item.sha256) != 64:
        raise BackupError("백업 파일 크기 또는 체크섬이 올바르지 않습니다.")
    if item.category not in {"asset", "recovery"} or (
        item.asset_id is not None
        and (not isinstance(item.asset_id, str) or len(item.asset_id) != 32)
    ):
        raise BackupError("백업 파일 자산 분류가 올바르지 않습니다.")
    return item


def _database_assets(
    path: Path, canceled: Callable[[], bool] | None = None
) -> dict[str, tuple[str, str, int | None, str | None]]:
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='file_assets'"
        ).fetchone():
            return {}
        _check_asset_references(connection)
        _raise_if_canceled(canceled)
        connection.set_progress_handler(lambda: int(canceled is not None and canceled()), 1000)
        return {
            str(row[0]): (str(row[1]), str(row[2]), row[3], row[4])
            for row in connection.execute(
                "SELECT relative_path,id,kind,fixed_size_bytes,fixed_checksum FROM file_assets WHERE state='active'"
            )
        }


def _verify_asset_captures(
    assets: dict[str, tuple[str, str, int | None, str | None]],
    items: tuple[BackupFile, ...],
    *,
    strict: bool = True,
) -> None:
    by_path = {item.path: item for item in items}
    if (
        strict
        and assets
        and any(item.category == "asset" and item.asset_id is None for item in items)
    ):
        raise BackupError("미분류 파일을 정식 자산으로 복원할 수 없습니다.")
    if strict and {
        item.path for item in items if item.category == "asset" and item.asset_id is not None
    } != set(assets):
        raise BackupError("자산 원장과 백업 파일 분류가 일치하지 않습니다.")
    for path, (asset_id, kind, size, checksum) in assets.items():
        item = by_path.get(path)
        if item is None and not strict:
            continue
        if item is None or (strict and (item.asset_id != asset_id or item.category != "asset")):
            raise BackupError(f"자산 원장과 백업 명세가 일치하지 않습니다: {path}")
        if kind == "frozen" and (item.size != size or item.sha256 != checksum):
            raise BackupSourceIncompleteError(
                f"보관 후 내용이 변경되어 백업하지 않았습니다: {path}"
            )
