"""Read-only, bounded inspection. A normal editable-file change is not corruption."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol


@dataclass(frozen=True, slots=True)
class InspectionTarget:
    asset_id: str
    relative_path: str
    name: str
    kinds: str
    frozen: bool
    size: int | None
    checksum: str | None
    reference_mismatch: bool = False


@dataclass(frozen=True, slots=True)
class InspectionIssue:
    target: InspectionTarget
    status: str


@dataclass(frozen=True, slots=True)
class InspectionReport:
    checked: int
    total: int
    counts: dict[str, int]
    issues: tuple[InspectionIssue, ...]
    omitted: int
    canceled: bool
    completed_at: datetime


class InspectionRepository(Protocol):
    def scope(self) -> tuple[str, int]: ...
    def page(self, after: str, upper: str) -> tuple[InspectionTarget, ...]: ...
    def current(self, asset_id: str) -> InspectionTarget | None: ...


class InspectionReader(Protocol):
    def inspect(self, target: InspectionTarget, canceled: Callable[[], bool]) -> str: ...


ISSUE_PRIORITY = {
    "missing": 0,
    "frozen_changed": 0,
    "reference_mismatch": 0,
    "unavailable": 1,
    "modified": 2,
    "no_baseline": 3,
}


class DataInspectionService:
    def __init__(self, repository: InspectionRepository, reader: InspectionReader) -> None:
        self.repository = repository
        self.reader = reader

    def run(
        self, canceled: Callable[[], bool], progress: Callable[[int, int], None] | None = None
    ) -> InspectionReport:
        upper, total = self.repository.scope()
        after = ""
        checked = 0
        counts: Counter[str] = Counter()
        issues: list[InspectionIssue] = []
        omitted = 0
        last_progress = 0.0
        while not canceled():
            page = self.repository.page(after, upper)
            if not page:
                break
            for target in page:
                if canceled():
                    break
                current = self.repository.current(target.asset_id)
                status = (
                    "unavailable" if current != target else self.reader.inspect(target, canceled)
                )
                if canceled():
                    break
                if self.repository.current(target.asset_id) != target:
                    status = "unavailable"
                checked += 1
                counts[status] += 1
                if status != "normal":
                    issue = InspectionIssue(target, status)
                    if len(issues) < 500:
                        issues.append(issue)
                    else:
                        # Informational editable changes must not hide missing/frozen problems.
                        worst = max(
                            range(len(issues)),
                            key=lambda index: ISSUE_PRIORITY[issues[index].status],
                        )
                        if ISSUE_PRIORITY[status] < ISSUE_PRIORITY[issues[worst].status]:
                            issues[worst] = issue
                        omitted += 1
                after = target.asset_id
                if progress and (checked == 1 or monotonic() - last_progress >= 0.1):
                    progress(checked, total)
                    last_progress = monotonic()
        return InspectionReport(
            checked,
            total,
            dict(counts),
            tuple(sorted(issues, key=lambda issue: ISSUE_PRIORITY[issue.status])),
            omitted,
            canceled(),
            datetime.now(UTC),
        )
