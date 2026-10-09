"""Bounded read-only history and literal, scope-compatible comparisons."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from officeflow.domain.task_history import HistoryEntry, HistoryPage


@dataclass(frozen=True, slots=True)
class HistoryQuery:
    scope_keys: tuple[str, ...] = ()
    event_type: str | None = None
    changed_field: str | None = None
    after: datetime | None = None
    before: datetime | None = None
    cursor: tuple[datetime, int, int] | None = None
    limit: int = 50

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 50:
            raise ValueError("이력은 한 페이지에 최대 50건입니다.")
        if self.after is not None and self.before is not None and self.after >= self.before:
            raise ValueError("조회 시작은 종료보다 빨라야 합니다.")


class HistoryRepository(Protocol):
    def subscribe(self, callback: Callable[[int], None]) -> Callable[[], None]: ...
    def page(self, task_id: int, query: HistoryQuery) -> HistoryPage: ...
    def get(self, task_id: int, entry_id: int, kind: str = "history") -> HistoryEntry: ...
    def current(self, task_id: int, occurrence_start: datetime | None) -> dict[str, Any]: ...
    def compare(
        self, task_id: int, left: tuple[str, int], right: tuple[str, int]
    ) -> tuple[HistoryEntry, HistoryEntry]: ...
    def compare_current(
        self, task_id: int, identity: tuple[str, int]
    ) -> tuple[HistoryEntry, dict[str, Any], datetime]: ...


class TaskHistoryService:
    def __init__(self, repository: HistoryRepository) -> None:
        self._repository = repository

    def subscribe(self, callback: Callable[[int], None]) -> Callable[[], None]:
        return self._repository.subscribe(callback)

    def page(self, task_id: int, query: HistoryQuery | None = None) -> HistoryPage:
        return self._repository.page(task_id, query or HistoryQuery())

    def get(self, task_id: int, entry_id: int, kind: str = "history") -> HistoryEntry:
        return self._repository.get(task_id, entry_id, kind)

    def compare(
        self, task_id: int, left_id: int | tuple[str, int], right_id: int | tuple[str, int]
    ) -> tuple[HistoryEntry, HistoryEntry]:
        left_kind, left_identity = left_id if isinstance(left_id, tuple) else ("history", left_id)
        right_kind, right_identity = (
            right_id if isinstance(right_id, tuple) else ("history", right_id)
        )
        left, right = self._repository.compare(
            task_id, (left_kind, left_identity), (right_kind, right_identity)
        )
        if left.summary.scope_key != right.summary.scope_key:
            raise ValueError("업무 공통끼리 또는 같은 회차끼리 선택하세요.")
        return left, right

    def current(self, task_id: int, occurrence_start: datetime | None = None) -> dict[str, Any]:
        return self._repository.current(task_id, occurrence_start)

    def compare_current(
        self, task_id: int, identity: tuple[str, int]
    ) -> tuple[HistoryEntry, dict[str, Any], datetime]:
        return self._repository.compare_current(task_id, identity)
