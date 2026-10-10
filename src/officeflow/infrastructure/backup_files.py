"""Read verified backup metadata; export independent copies, never restore live data."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import tempfile
import zipfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from officeflow.application.attachment_export import (
    ExportBatchResult,
    ExportItemResult,
    ExportStatus,
    FileReference,
    PreparedExport,
    ResolvedFileReference,
)
from officeflow.application.attachments import AttachmentCanceledError
from officeflow.application.file_operations import FileJobCanceledError
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
from officeflow.infrastructure.attachments.read_lease import file_identity, protected_read
from officeflow.infrastructure.backup import (
    BackupCanceledError,
    BackupError,
    BackupManager,
    _backup_member,
    _copy_stream,
)

CHUNK = 1024 * 1024
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BackupFileHit:
    id: int
    kind: str
    name: str
    label: str
    memo: str
    task_title: str
    member: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BackupFilePage:
    items: tuple[BackupFileHit, ...]
    has_more: bool


def _cancel(canceled: Callable[[], bool]) -> None:
    if canceled():
        raise BackupCanceledError("백업 자료 조회/저장을 취소했습니다.")


class BackupFileSession:
    """Owned temporary catalogue + captured archive identity; worker calls are serialized."""

    def __init__(self, manager: BackupManager, archive_path: Path) -> None:
        self.manager = manager
        self.archive_path = archive_path.resolve()
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._identity: tuple[int, int, int, int] | None = None
        self.created_at = ""

    @property
    def available(self) -> bool:
        return self._temporary is not None and self._identity is not None

    @property
    def root(self) -> Path:
        if self._temporary is None:
            raise BackupError("먼저 백업 파일을 선택하세요.")
        return Path(self._temporary.name)

    def close(self) -> None:
        if self._temporary is not None:
            try:
                self._temporary.cleanup()
            except OSError as error:
                # Temporary owned files only. Do not fail application shutdown or
                # delete the source/output in response to a security-product lock.
                logger.warning("Backup file temporary cleanup deferred: %s", type(error).__name__)
            self._temporary = None
        self._identity = None

    def load(self, canceled: Callable[[], bool]) -> BackupFilePage:
        try:
            with self.manager._coordinator.job(canceled):
                return self._load(canceled)
        except FileJobCanceledError as error:
            raise BackupCanceledError("백업 자료 대기 중 취소했습니다.") from error

    def _load(self, canceled: Callable[[], bool]) -> BackupFilePage:
        if self._temporary is not None:
            raise BackupError("이미 열린 백업입니다.")
        try:
            with protected_read(self.archive_path) as stream, zipfile.ZipFile(stream) as archive:
                self._identity = file_identity(os.fstat(stream.fileno()))
                # Preflight before the full verifier reads/decompresses payloads.
                infos = archive.infolist()
                if len(infos) > 100_000:
                    raise BackupError("이 화면의 백업 파일 수 한도(100,000개)를 초과했습니다.")
                if archive.getinfo("manifest.json").file_size > 16 * 1024**2:
                    raise BackupError("백업 명세가 너무 큽니다.")
                if archive.getinfo("data/officeflow.db").file_size > 512 * 1024**2:
                    raise BackupError("선택 조회용 DB 한도(512MB)를 초과했습니다.")
                manifest = self.manager.verify_backup(
                    self.archive_path, cancel_requested=canceled, check_schema=True
                )
                _cancel(canceled)
                self._temporary = tempfile.TemporaryDirectory(
                    prefix=".backup-files-", dir=self.manager.backup_directory.parent
                )
                database = self.root / "source.db"
                with archive.open("data/officeflow.db") as source, database.open("xb") as sink:
                    _copy_stream(source, sink, canceled)
                if file_identity(self.archive_path.stat()) != self._identity:
                    raise BackupError("백업 파일이 변경되었습니다. 다시 선택하세요.")
                self.created_at = manifest.created_at
                files = {item.path: item for item in manifest.attachments}
                with closing(
                    sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
                ) as source_db:
                    source_db.set_progress_handler(lambda: int(canceled()), 1000)
                    tables = {
                        row[0]
                        for row in source_db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    with closing(sqlite3.connect(self.root / "catalog.db")) as catalog:
                        catalog.execute(
                            "CREATE TABLE files(id INTEGER PRIMARY KEY,kind TEXT,name TEXT,label TEXT,memo TEXT,task_title TEXT,member TEXT,size INTEGER,sha256 TEXT)"
                        )
                        used: set[str] = set()
                        logical_count = 0
                        queries = self._queries(source_db, tables)
                        for query in queries:
                            for kind, name, label, memo, title, relative in source_db.execute(
                                query
                            ):
                                _cancel(canceled)
                                item = files.get(relative)
                                if item is None:
                                    # Old backups may explicitly declare a missing attachment.
                                    continue
                                logical_count += 1
                                if logical_count > 100_000:
                                    raise BackupError(
                                        "선택 조회 항목 한도(100,000개)를 초과했습니다."
                                    )
                                used.add(relative)
                                catalog.execute(
                                    "INSERT INTO files(kind,name,label,memo,task_title,member,size,sha256) VALUES(?,?,?,?,?,?,?,?)",
                                    (
                                        kind,
                                        name,
                                        label,
                                        memo[:2000],
                                        title,
                                        _backup_member(item, manifest),
                                        item.size,
                                        item.sha256,
                                    ),
                                )
                        for relative, item in files.items():
                            _cancel(canceled)
                            if relative not in used:
                                logical_count += 1
                                if logical_count > 100_000:
                                    raise BackupError(
                                        "선택 조회 항목 한도(100,000개)를 초과했습니다."
                                    )
                                catalog.execute(
                                    "INSERT INTO files(kind,name,label,memo,task_title,member,size,sha256) VALUES(?,?,?,?,?,?,?,?)",
                                    (
                                        "recovery",
                                        Path(relative).name,
                                        "",
                                        "",
                                        "",
                                        _backup_member(item, manifest),
                                        item.size,
                                        item.sha256,
                                    ),
                                )
                        catalog.commit()
                return self.page(canceled=canceled)
        except Exception as error:
            self.close()
            _cancel(canceled)
            if isinstance(error, BackupError):
                raise
            raise BackupError(
                "백업 자료를 열지 못했습니다. 파일 형식·권한·저장 공간을 확인하세요."
            ) from error

    @staticmethod
    def _queries(connection: sqlite3.Connection, tables: set[str]) -> list[str]:
        cols = {row[1] for row in connection.execute("PRAGMA table_info(attachments)")}
        name = "a.display_name" if "display_name" in cols else "a.original_name"
        memo = "a.memo" if "memo" in cols else "''"
        relative = "f.relative_path" if "file_assets" in tables else "a.relative_path"
        asset_join = "JOIN file_assets f ON f.id=a.asset_id" if "file_assets" in tables else ""
        queries = [
            f"SELECT 'attachment',{name},'',{memo},t.title,{relative} FROM attachments a JOIN tasks t ON t.id=a.task_id {asset_join} ORDER BY a.created_at DESC,a.id DESC"
        ]
        if "attachment_versions" in tables:
            queries.append(
                "SELECT 'version',v.original_name,v.label,v.memo,t.title,f.relative_path FROM attachment_versions v JOIN tasks t ON t.id=v.task_id JOIN file_assets f ON f.id=v.asset_id ORDER BY v.created_at DESC,v.id DESC"
            )
        if "snapshot_files" in tables:
            queries.append(
                "SELECT 'snapshot_file',s.name,p.label,s.memo,t.title,f.relative_path FROM snapshot_files s JOIN task_snapshots p ON p.id=s.snapshot_id JOIN tasks t ON t.id=p.task_id JOIN file_assets f ON f.id=s.asset_id ORDER BY p.captured_at DESC,s.id DESC"
            )
        return queries

    def page(
        self, search: str = "", after: int = 0, *, canceled: Callable[[], bool]
    ) -> BackupFilePage:
        _cancel(canceled)
        pattern = (
            "%" + search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        with closing(
            sqlite3.connect((self.root / "catalog.db").as_uri() + "?mode=ro", uri=True)
        ) as db:
            db.set_progress_handler(lambda: int(canceled()), 1000)
            rows = db.execute(
                "SELECT id,kind,name,label,substr(memo,1,500),task_title,member,size,sha256 FROM files WHERE id>? AND (name LIKE ? ESCAPE '\\' OR label LIKE ? ESCAPE '\\' OR memo LIKE ? ESCAPE '\\' OR task_title LIKE ? ESCAPE '\\') ORDER BY id LIMIT 101",
                (after, pattern, pattern, pattern, pattern),
            ).fetchall()
        _cancel(canceled)
        return BackupFilePage(tuple(BackupFileHit(*row) for row in rows[:100]), len(rows) > 100)

    def export(
        self,
        identities: tuple[int, ...],
        folder: Path,
        canceled: Callable[[], bool],
        progress: Callable[[int, int], None] | None = None,
    ) -> ExportBatchResult:
        if not identities or len(identities) > 100 or len(set(identities)) != len(identities):
            raise ValueError("이 페이지에서 저장할 파일 1~100개를 선택하세요.")
        try:
            with self.manager._coordinator.job(canceled):
                return self._export(identities, folder, canceled, progress)
        except FileJobCanceledError:
            return ExportBatchResult(
                "",
                tuple(
                    ExportItemResult(
                        FileReference.orphan(f"backup/{identity}"),
                        "",
                        ExportStatus.UNPROCESSED,
                        message="대기 중 취소",
                    )
                    for identity in identities
                ),
                folder,
                True,
            )

    def _export(
        self,
        identities: tuple[int, ...],
        folder: Path,
        canceled: Callable[[], bool],
        progress: Callable[[int, int], None] | None = None,
    ) -> ExportBatchResult:
        if not identities or len(identities) > 100 or len(set(identities)) != len(identities):
            raise ValueError("이 페이지에서 저장할 파일 1~100개를 선택하세요.")
        with closing(
            sqlite3.connect((self.root / "catalog.db").as_uri() + "?mode=ro", uri=True)
        ) as db:
            hits = []
            for identity in identities:
                row = db.execute("SELECT * FROM files WHERE id=?", (identity,)).fetchone()
                if row is None:
                    raise BackupError("선택 목록이 변경되었습니다. 다시 조회하세요.")
                hits.append(BackupFileHit(*row))
        storage = ExternalExportStorage(self.manager.backup_directory.parent)
        job = storage.new_job_id()
        results: list[ExportItemResult] = []
        stop = False
        with protected_read(self.archive_path) as stream, zipfile.ZipFile(stream) as archive:
            if (
                file_identity(os.fstat(stream.fileno())) != self._identity
                or file_identity(self.archive_path.stat()) != self._identity
            ):
                raise BackupError("백업 파일이 변경되었습니다. 다시 선택하세요.")
            for index, hit in enumerate(hits):
                ref = FileReference.orphan(f"backup/{hit.id}")
                if stop or canceled():
                    results.append(
                        ExportItemResult(
                            ref, hit.name, ExportStatus.UNPROCESSED, message="취소 또는 저장 중단"
                        )
                    )
                    continue
                prepared: PreparedExport | None = None
                extracted = self.root / f"selected-{hit.id}{Path(hit.name).suffix[:17]}"
                try:
                    _cancel(canceled)
                    if archive.getinfo(hit.member).file_size != hit.size:
                        raise BackupError("백업 파일 크기가 일치하지 않습니다.")
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(hit.member) as source, extracted.open("xb") as target:
                        while chunk := source.read(CHUNK):
                            _cancel(canceled)
                            target.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                            if size > hit.size:
                                raise BackupError("백업 파일 크기가 일치하지 않습니다.")
                    if size != hit.size or digest.hexdigest() != hit.sha256:
                        raise BackupError("백업 파일 체크섬이 일치하지 않습니다.")
                    name = hit.name
                    if hit.kind == "version":
                        from officeflow.application.attachment_export import AttachmentExportService

                        name = AttachmentExportService._version_name(name, hit.label)
                    destination = storage.validate_destination(
                        folder / storage.safe_name(name), extracted
                    )
                    resolved = ResolvedFileReference(ref, extracted, name, f"backup/{hit.id}")
                    prepared = storage.prepare(
                        resolved, destination, job, cancel_requested=canceled, progress=None
                    )
                    output, warning = storage.publish(
                        prepared, numbered=True, cancel_requested=canceled, confirm_overwrite=None
                    )
                    results.append(
                        ExportItemResult(
                            ref,
                            name,
                            ExportStatus.SAVED,
                            output,
                            message=warning,
                            journal_path=prepared.journal if warning else None,
                        )
                    )
                except (BackupCanceledError, AttachmentCanceledError):
                    if prepared is not None:
                        try:
                            storage.abort(prepared)
                        except Exception:
                            results.append(
                                ExportItemResult(
                                    ref,
                                    hit.name,
                                    ExportStatus.NEEDS_REVIEW,
                                    message="취소했지만 준비 파일 정리가 보류되었습니다. 데이터 → 첨부 정리 → 중단된 저장 확인에서 확인하세요.",
                                    journal_path=prepared.journal,
                                )
                            )
                            stop = True
                            continue
                    results.append(
                        ExportItemResult(ref, hit.name, ExportStatus.UNPROCESSED, message="취소됨")
                    )
                    stop = True
                except Exception:
                    review = prepared is not None and prepared.journal.exists()
                    results.append(
                        ExportItemResult(
                            ref,
                            hit.name,
                            ExportStatus.NEEDS_REVIEW if review else ExportStatus.FAILED,
                            message="저장하지 못했습니다. 파일 검증·권한·공간을 확인하세요. 복구 기록이 있으면 데이터 → 첨부 정리 → 중단된 저장 확인에서 확인하세요.",
                            journal_path=prepared.journal if review and prepared else None,
                        )
                    )
                    stop = True
                finally:
                    try:
                        extracted.unlink(missing_ok=True)
                    except OSError as error:
                        logger.warning(
                            "Backup selected temporary cleanup deferred: %s", type(error).__name__
                        )
                if progress:
                    progress(index + 1, len(hits))
        return ExportBatchResult(job, tuple(results), folder, canceled())
