from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RecentTaskRef:
    task_id: int
    task_created_at: datetime
    occurrence_start: datetime | None
    viewed_at: datetime


@dataclass(frozen=True, slots=True)
class RecentTaskSummary:
    task_id: int
    title: str
    status: str
    created_at: datetime
    is_pinned: bool


class RecentTaskRepository(Protocol):
    def summaries(self, ids: tuple[int, ...]) -> dict[int, RecentTaskSummary]: ...
    def pinned(self) -> tuple[RecentTaskSummary, ...]: ...


class RecentTasks:
    LIMIT = 20

    def __init__(self, repository: RecentTaskRepository) -> None:
        self.repository = repository
        self.entries: tuple[RecentTaskRef, ...] = ()

    def record_viewed(self, ref: RecentTaskRef) -> None:
        self.entries = (ref, *(r for r in self.entries if r.task_id != ref.task_id))[: self.LIMIT]

    def resolve(
        self, refs: tuple[RecentTaskRef, ...]
    ) -> tuple[tuple[RecentTaskSummary, RecentTaskRef], ...]:
        refs = refs[: self.LIMIT]
        current = self.repository.summaries(tuple(r.task_id for r in refs))
        return tuple(
            (current[r.task_id], r)
            for r in refs
            if r.task_id in current and current[r.task_id].created_at == r.task_created_at
        )

    @staticmethod
    def reference(summary: RecentTaskSummary) -> RecentTaskRef:
        return RecentTaskRef(summary.task_id, summary.created_at, None, datetime.now(UTC))
