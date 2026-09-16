from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class LegacyMigrationError(RuntimeError):
    """Raised when a legacy database cannot be inspected or imported safely."""


@dataclass(frozen=True, slots=True)
class MigrationIssue:
    level: str
    code: str
    message: str
    table: str | None = None
    legacy_id: int | None = None


@dataclass(frozen=True, slots=True)
class MigrationCounts:
    tasks: int = 0
    work_logs: int = 0
    notes: int = 0
    attachments: int = 0


@dataclass(frozen=True, slots=True)
class MigrationPreview:
    source_database: Path
    source_sha256: str
    attachment_root: Path | None
    source_counts: MigrationCounts
    importable_counts: MigrationCounts
    missing_attachments: int
    orphan_work_logs: int
    orphan_attachments: int
    issues: tuple[MigrationIssue, ...]

    @property
    def can_import(self) -> bool:
        return not any(issue.level == "error" for issue in self.issues)


@dataclass(frozen=True, slots=True)
class MigrationResult:
    source_backup: Path
    pending_restore: Path
    report_path: Path
    imported_counts: MigrationCounts
    skipped_tasks: int
    missing_attachments: int


class LegacyMigration(Protocol):
    def preview(
        self,
        source_database: Path,
        attachment_root: Path | None = None,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> MigrationPreview: ...

    def migrate(
        self,
        preview: MigrationPreview,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> MigrationResult: ...
