"""Short read-only SQLite pages and independent streamed file reads."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from officeflow.application.attachments import AttachmentOperationError
from officeflow.application.data_inspection import InspectionTarget
from officeflow.application.file_operations import (
    FileInUseError,
    FileJobCanceledError,
    FileOperationCoordinator,
)
from officeflow.infrastructure.attachments.internal_storage import checked_managed_path
from officeflow.infrastructure.attachments.read_lease import file_identity, protected_read

_REFERENCED = """f.state='active' AND (
EXISTS(SELECT 1 FROM attachments a WHERE a.asset_id=f.id) OR
EXISTS(SELECT 1 FROM attachment_versions v WHERE v.asset_id=f.id) OR
EXISTS(SELECT 1 FROM snapshot_files s WHERE s.asset_id=f.id))"""
_TARGET = (
    """SELECT f.id, f.relative_path,
COALESCE((SELECT a.display_name FROM attachments a WHERE a.asset_id=f.id LIMIT 1),
(SELECT v.original_name FROM attachment_versions v WHERE v.asset_id=f.id LIMIT 1),
(SELECT s.name FROM snapshot_files s WHERE s.asset_id=f.id LIMIT 1), f.stored_name),
EXISTS(SELECT 1 FROM attachments a WHERE a.asset_id=f.id),
EXISTS(SELECT 1 FROM attachment_versions v WHERE v.asset_id=f.id),
EXISTS(SELECT 1 FROM snapshot_files s WHERE s.asset_id=f.id),
f.kind,
CASE WHEN f.kind='frozen' THEN f.fixed_size_bytes ELSE COALESCE(f.observed_size_bytes,
(SELECT a.size_bytes FROM attachments a WHERE a.asset_id=f.id LIMIT 1)) END,
CASE WHEN f.kind='frozen' THEN f.fixed_checksum ELSE COALESCE(f.observed_checksum,
(SELECT a.checksum FROM attachments a WHERE a.asset_id=f.id LIMIT 1)) END,
EXISTS(SELECT 1 FROM snapshot_files s WHERE s.asset_id=f.id AND
(s.fixed_checksum!=f.fixed_checksum OR s.fixed_size_bytes!=f.fixed_size_bytes OR f.kind!='frozen'))
FROM file_assets f WHERE """
    + _REFERENCED
)


class SqliteInspectionRepository:
    def __init__(self, database: Path) -> None:
        self.database = database
        self._high_water = 0
        self._scope_time = ""

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2
        )
        try:
            yield connection
        finally:
            connection.close()

    def scope(self) -> tuple[str, int]:
        self._scope_time = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(f.id),''),COUNT(*),COALESCE(MAX(f.rowid),0) FROM file_assets f WHERE "
                + _REFERENCED
                + " AND f.created_at<=?",
                (self._scope_time,),
            ).fetchone()
            self._high_water = int(row[2])
            return str(row[0]), int(row[1])

    @staticmethod
    def _target(row: tuple[object, ...]) -> InspectionTarget:
        kinds = " · ".join(
            label
            for exists, label in zip(row[3:6], ("첨부", "개별 보관", "중요 보관"), strict=True)
            if exists
        )
        return InspectionTarget(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            kinds,
            row[6] == "frozen",
            int(str(row[7])) if row[7] is not None else None,
            str(row[8]) if row[8] is not None else None,
            bool(row[9]),
        )

    def page(self, after: str, upper: str) -> tuple[InspectionTarget, ...]:
        with self._connection() as connection:
            return tuple(
                self._target(row)
                for row in connection.execute(
                    _TARGET
                    + " AND f.id>? AND f.id<=? AND f.rowid<=? AND f.created_at<=? ORDER BY f.id LIMIT 100",
                    (after, upper, self._high_water, self._scope_time),
                )
            )

    def current(self, asset_id: str) -> InspectionTarget | None:
        with self._connection() as connection:
            row = connection.execute(_TARGET + " AND f.id=?", (asset_id,)).fetchone()
            return self._target(row) if row else None


class ManagedInspectionReader:
    def __init__(self, root: Path, coordinator: FileOperationCoordinator) -> None:
        self.root = root.resolve()
        self.coordinator = coordinator

    def inspect(self, target: InspectionTarget, canceled: Callable[[], bool]) -> str:
        try:
            with self.coordinator.job(canceled), self.coordinator.pin(target.relative_path):
                if target.reference_mismatch:
                    return "reference_mismatch"
                path = checked_managed_path(self.root, target.relative_path, file_required=True)
                with protected_read(path) as stream:
                    before = os.fstat(stream.fileno())
                    digest = hashlib.sha256()
                    while not canceled():
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                    if canceled():
                        return "unavailable"
                    if file_identity(before) != file_identity(
                        os.fstat(stream.fileno())
                    ) or file_identity(before) != file_identity(path.stat()):
                        return "unavailable"
                if target.checksum is None or target.size is None:
                    return "unavailable" if target.frozen else "no_baseline"
                unchanged = (before.st_size, digest.hexdigest()) == (target.size, target.checksum)
                return "normal" if unchanged else "frozen_changed" if target.frozen else "modified"
        except FileNotFoundError:
            return "missing"
        except (OSError, AttachmentOperationError, FileInUseError, FileJobCanceledError):
            return "unavailable"
