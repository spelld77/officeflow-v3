"""Numeric observations only; a failed observation must not stop essential work."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ResourceState(StrEnum):
    NORMAL = "normal"
    PRESSURE = "pressure"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    sampled_at: float
    working_bytes: int | None = None
    private_bytes: int | None = None
    handles: int | None = None
    gdi: int | None = None
    user: int | None = None
    commit_bytes: int | None = None
    commit_limit: int | None = None
    physical_available: int | None = None
    physical_total: int | None = None
    low_memory: bool | None = None

    @property
    def commit_ratio(self) -> float | None:
        if (
            self.commit_limit
            and self.commit_bytes is not None
            and 0 <= self.commit_bytes <= self.commit_limit
        ):
            return self.commit_bytes / self.commit_limit
        return None


class ResourcePolicy:
    def __init__(self) -> None:
        self.state = ResourceState.UNKNOWN
        self._high = self._low = 0
        self._last_valid: float | None = None

    def observe(self, snapshot: ResourceSnapshot) -> ResourceState:
        ratio = snapshot.commit_ratio
        if ratio is None or snapshot.low_memory is None:
            self._high = self._low = 0
            if self._last_valid is None or snapshot.sampled_at - self._last_valid >= 120:
                self.state = ResourceState.UNKNOWN
            return self.state
        self._last_valid = snapshot.sampled_at
        high = snapshot.low_memory or ratio >= 0.92
        self._high = self._high + 1 if high else 0
        self._low = self._low + 1 if not snapshot.low_memory and ratio < 0.85 else 0
        if ratio >= 0.97 or self._high >= 2:
            self.state = ResourceState.PRESSURE
        elif self.state is not ResourceState.PRESSURE or self._low >= 3:
            self.state = ResourceState.NORMAL
        return self.state


class IdleBackoff:
    INTERVALS = (1, 2, 5, 15, 30, 60)

    def __init__(self) -> None:
        self.level = 0

    def next_seconds(self, *, progressed: bool, due_in: float | None = None) -> float:
        if progressed:
            self.level = 0
        value = self.INTERVALS[self.level]
        if not progressed:
            self.level = min(self.level + 1, len(self.INTERVALS) - 1)
        return max(1.0, min(value, due_in)) if due_in is not None else float(value)
