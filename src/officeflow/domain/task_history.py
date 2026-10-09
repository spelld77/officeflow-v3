"""Versioned literal values, independent of today's task and attachment names."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from officeflow.domain.occurrence import TaskOccurrence
from officeflow.domain.recurrence import recurrence_until_utc
from officeflow.domain.task import Task

FIELDS = ("title", "description", "schedule", "priority", "status", "result_note")
FIELD_LABELS = dict(
    zip(FIELDS, ("제목", "설명", "일정", "중요도", "상태", "결과메모"), strict=True)
)


def utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("시간대가 없는 이력 시각입니다.")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def scope_key(occurrence_start: datetime | None) -> str:
    return "task" if occurrence_start is None else f"occ:{utc_text(occurrence_start)}"


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return utc_text(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_json_value(item) for item in value)
    return value


def encode(value: Any) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def task_values(task: Task, occurrence: TaskOccurrence | None = None) -> dict[str, Any]:
    schedule: dict[str, Any] = {
        "all_day": task.all_day,
        "starts_at": utc_text(task.starts_at),
        "ends_at": utc_text(task.ends_at),
        "timezone": task.timezone,
        "recurrence_rule": task.recurrence_rule,
        "recurrence_until": utc_text(recurrence_until_utc(task.recurrence_rule)),
    }
    if occurrence is not None:
        schedule.update(
            {
                "occurrence_start": utc_text(occurrence.occurrence_start),
                "occurrence_end": utc_text(occurrence.occurrence_end),
                "effective_start": utc_text(occurrence.effective_start),
                "effective_end": utc_text(occurrence.effective_end),
            }
        )
    state = occurrence if occurrence is not None else task
    return {
        "title": task.title,
        "description": task.description,
        "schedule": schedule,
        "priority": task.priority.value,
        "status": {"value": state.status.value, "completed_at": utc_text(state.completed_at)},
        "result_note": state.result_note,
    }


@dataclass(frozen=True, slots=True)
class HistorySummary:
    id: int
    task_id: int
    scope_key: str
    occurrence_start: datetime | None
    event_type: str
    recorded_at: datetime
    changed_fields: tuple[str, ...]
    summary: str
    kind: str = "history"
    file_count: int = 0


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    summary: HistorySummary
    command_id: str
    before: dict[str, Any] | None
    after: dict[str, Any]
    saved_info: dict[str, Any] | None = None

    @property
    def changed_fields(self) -> tuple[str, ...]:
        return self.summary.changed_fields


@dataclass(frozen=True, slots=True)
class HistoryPage:
    items: tuple[HistorySummary, ...]
    has_next: bool


class ReplayedNote(str):
    """A retried memo is a current read; the marker is not persisted as task data."""

    command_replayed = True
