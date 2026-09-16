from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from officeflow.bootstrap.paths import AppPaths

_FORMAT_VERSION = 1


class BackupError(RuntimeError):
    """Raised when a backup cannot be created, verified, or restored safely."""


@dataclass(frozen=True, slots=True)
class BackupFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BackupManifest:
    format_version: int
    created_at: str
    reason: str
    database_sha256: str
    table_counts: dict[str, int]
    attachments: tuple[BackupFile, ...]
    settings: BackupFile | None = None


@dataclass(frozen=True, slots=True)
class BackupInfo:
    path: Path
    created_at: datetime
    reason: str
    table_counts: dict[str, int]
    attachment_count: int


class BackupManager:
    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths

    def create_backup(
        self,
        *,
        reason: str = "manual",
        keep: int | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> BackupInfo:
        if not self._paths.database_file.is_file():
            raise BackupError("백업할 OfficeFlow 데이터베이스가 없습니다.")
        self._paths.ensure_directories()
        created_at = datetime.now(UTC)
        stamp = created_at.astimezone().strftime("%Y%m%d-%H%M%S")
        safe_reason = reason if reason in {"manual", "automatic", "pre-restore", "pre-import"} else "manual"
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
                self._snapshot_database(snapshot, cancel_requested)
                table_counts = _database_counts(snapshot)
                attachments = self._attachment_files(cancel_requested)
                settings = (
                    BackupFile(
                        path="settings.json",
                        size=self._paths.settings_file.stat().st_size,
                        sha256=_sha256(self._paths.settings_file, cancel_requested),
                    )
                    if self._paths.settings_file.is_file()
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
                )
                with zipfile.ZipFile(
                    temporary_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
                ) as archive:
                    _write_archive_file(
                        archive, snapshot, "data/officeflow.db", cancel_requested
                    )
                    for item in attachments:
                        _write_archive_file(
                            archive,
                            self._paths.attachment_dir / PurePosixPath(item.path),
                            f"attachments/{item.path}",
                            cancel_requested,
                        )
                    if settings is not None:
                        _write_archive_file(
                            archive,
                            self._paths.settings_file,
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
        except (OSError, sqlite3.Error, zipfile.BadZipFile, ValueError) as error:
            raise BackupError(f"백업을 만들지 못했습니다: {error}") from error
        finally:
            temporary_archive.unlink(missing_ok=True)
        if safe_reason == "automatic" and keep is not None:
            self.prune_automatic_backups(keep)
        return BackupInfo(
            path=target,
            created_at=created_at,
            reason=safe_reason,
            table_counts=table_counts,
            attachment_count=len(attachments),
        )

    def verify_backup(
        self,
        archive_path: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> BackupManifest:
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                names = set(archive.namelist())
                if len(names) != len(archive.infolist()):
                    raise BackupError("백업에 중복된 파일 경로가 있습니다.")
                for name in names:
                    _validate_archive_name(name)
                if "manifest.json" not in names or "data/officeflow.db" not in names:
                    raise BackupError("백업에 필수 파일이 없습니다.")
                manifest = _parse_manifest(archive.read("manifest.json"))
                expected = {"manifest.json", "data/officeflow.db"}
                expected.update(f"attachments/{item.path}" for item in manifest.attachments)
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
                    member = f"attachments/{item.path}"
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
                    if _database_counts(database) != manifest.table_counts:
                        raise BackupError("백업 데이터베이스의 레코드 개수가 일치하지 않습니다.")
                return manifest
        except BackupError:
            raise
        except (OSError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as error:
            raise BackupError(f"백업 검증에 실패했습니다: {error}") from error

    def stage_restore(
        self,
        archive_path: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> BackupManifest:
        source = archive_path.resolve()
        if source == self._paths.pending_restore_file.resolve():
            return self.verify_backup(source, cancel_requested=cancel_requested)
        manifest = self.verify_backup(source, cancel_requested=cancel_requested)
        pending = self._paths.pending_restore_file
        temporary = pending.with_suffix(".part")
        try:
            with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
                _copy_stream(input_stream, output_stream, cancel_requested)
            self.verify_backup(temporary, cancel_requested=cancel_requested)
            os.replace(temporary, pending)
        finally:
            temporary.unlink(missing_ok=True)
        return manifest

    def apply_pending_restore(self) -> BackupInfo | None:
        pending = self._paths.pending_restore_file
        if not pending.is_file():
            return None
        manifest = self.verify_backup(pending)
        self._paths.ensure_directories()
        pre_restore = self.create_backup(reason="pre-restore")
        rollback = self._paths.root / f".restore-rollback-{uuid.uuid4().hex}"
        try:
            with tempfile.TemporaryDirectory(
                prefix=".restore-stage-", dir=self._paths.root
            ) as staging_name:
                staging = Path(staging_name)
                self._extract_verified(pending, staging)
                rollback.mkdir()
                self._swap_restored_data(staging, rollback, manifest)
            pending.unlink()
            shutil.rmtree(rollback)
        except Exception as error:
            self._rollback_restore(rollback)
            raise BackupError(f"복원을 적용하지 못했습니다: {error}") from error
        return pre_restore

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
                progress=lambda _status, _remaining, _total: _raise_if_canceled(
                    cancel_requested
                ),
            )

    def _attachment_files(
        self, cancel_requested: Callable[[], bool] | None
    ) -> tuple[BackupFile, ...]:
        items: list[BackupFile] = []
        for path in sorted(self._paths.attachment_dir.rglob("*")):
            _raise_if_canceled(cancel_requested)
            relative_path = path.relative_to(self._paths.attachment_dir)
            if not path.is_file() or any(part.startswith(".") for part in relative_path.parts):
                continue
            relative = relative_path.as_posix()
            items.append(
                BackupFile(
                    path=relative,
                    size=path.stat().st_size,
                    sha256=_sha256(path, cancel_requested),
                )
            )
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

    def _swap_restored_data(
        self, staging: Path, rollback: Path, manifest: BackupManifest
    ) -> None:
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
        if self._paths.settings_file.exists():
            shutil.copy2(self._paths.settings_file, rollback / "settings.json")

        self._paths.data_dir.mkdir(parents=True, exist_ok=True)
        os.replace(staging / "data" / "officeflow.db", self._paths.database_file)
        staged_attachments = staging / "attachments"
        staged_attachments.mkdir(exist_ok=True)
        os.replace(staged_attachments, self._paths.attachment_dir)
        staged_settings = staging / "settings.json"
        if staged_settings.exists():
            os.replace(staged_settings, self._paths.settings_file)

        if _database_counts(self._paths.database_file) != manifest.table_counts:
            raise BackupError("복원된 데이터베이스의 레코드 개수가 일치하지 않습니다.")
        for item in manifest.attachments:
            restored = self._paths.attachment_dir / PurePosixPath(item.path)
            if not restored.is_file() or _sha256(restored) != item.sha256:
                raise BackupError(f"복원된 첨부파일 검증에 실패했습니다: {item.path}")

    def _rollback_restore(self, rollback: Path) -> None:
        if not rollback.exists():
            return
        old_data = rollback / "data"
        old_database = old_data / self._paths.database_file.name
        swap_started = old_database.exists()
        if old_database.exists():
            self._paths.database_file.unlink(missing_ok=True)
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
        elif swap_started:
            self._paths.settings_file.unlink(missing_ok=True)
        shutil.rmtree(rollback, ignore_errors=True)


def _database_counts(path: Path) -> dict[str, int]:
    try:
        with closing(
            sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        ) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise BackupError("SQLite 무결성 검사에 실패했습니다.")
            tables = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            counts: dict[str, int] = {}
            for (name,) in tables:
                if not isinstance(name, str):
                    raise BackupError("데이터베이스 테이블 이름이 올바르지 않습니다.")
                quoted = name.replace('"', '""')
                row = connection.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()
                if row is None:
                    raise BackupError(f"테이블 개수를 확인하지 못했습니다: {name}")
                counts[name] = int(row[0])
            return counts
    except sqlite3.Error as error:
        raise BackupError(f"SQLite 검증에 실패했습니다: {error}") from error


def _raise_if_canceled(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise BackupError("백업 작업을 취소했습니다.")


def _sha256(
    path: Path, cancel_requested: Callable[[], bool] | None = None
) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            _raise_if_canceled(cancel_requested)
            digest.update(chunk)
    return digest.hexdigest()


def _stream_digest(
    stream: Any, cancel_requested: Callable[[], bool] | None = None
) -> str:
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
    allowed = (
        name in {"manifest.json", "data/officeflow.db", "settings.json"}
        or (len(path.parts) >= 2 and path.parts[0] == "attachments")
    )
    if not allowed:
        raise BackupError(f"백업에 알 수 없는 파일이 있습니다: {name}")


def _manifest_payload(manifest: BackupManifest) -> dict[str, Any]:
    payload = asdict(manifest)
    payload["attachments"] = [asdict(item) for item in manifest.attachments]
    payload["settings"] = asdict(manifest.settings) if manifest.settings is not None else None
    return payload


def _parse_manifest(payload: bytes) -> BackupManifest:
    raw = json.loads(payload.decode("utf-8"))
    if not isinstance(raw, dict) or raw.get("format_version") != _FORMAT_VERSION:
        raise BackupError("지원하지 않는 백업 형식입니다.")
    attachments_raw = raw.get("attachments")
    counts_raw = raw.get("table_counts")
    if not isinstance(attachments_raw, list) or not isinstance(counts_raw, dict):
        raise BackupError("백업 명세가 올바르지 않습니다.")
    attachments = tuple(_parse_file(item) for item in attachments_raw)
    if len({item.path for item in attachments}) != len(attachments):
        raise BackupError("백업 명세에 중복된 첨부파일이 있습니다.")
    settings_raw = raw.get("settings")
    settings = _parse_file(settings_raw) if settings_raw is not None else None
    if settings is not None and settings.path != "settings.json":
        raise BackupError("백업 설정 파일 경로가 올바르지 않습니다.")
    try:
        counts = {str(key): int(value) for key, value in counts_raw.items()}
        manifest = BackupManifest(
            format_version=int(raw["format_version"]),
            created_at=str(raw["created_at"]),
            reason=str(raw["reason"]),
            database_sha256=str(raw["database_sha256"]),
            table_counts=counts,
            attachments=attachments,
            settings=settings,
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
        item = BackupFile(path=str(raw["path"]), size=int(raw["size"]), sha256=str(raw["sha256"]))
    except (KeyError, TypeError, ValueError) as error:
        raise BackupError("백업 파일 명세 필드가 올바르지 않습니다.") from error
    _validate_archive_name(f"attachments/{item.path}" if item.path != "settings.json" else item.path)
    if item.size < 0 or len(item.sha256) != 64:
        raise BackupError("백업 파일 크기 또는 체크섬이 올바르지 않습니다.")
    return item
