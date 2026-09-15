from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from officeflow.domain.enums import OccurrenceStatus


@dataclass(frozen=True, slots=True)
class TaskOccurrence:
    id: int | None
    task_id: int
    occurrence_start: datetime
    occurrence_end: datetime | None
    effective_start: datetime | None = None
    effective_end: datetime | None = None
    status: OccurrenceStatus = OccurrenceStatus.PENDING
    completed_at: datetime | None = None
    result_note: str = ""

    @property
    def starts_at(self) -> datetime:
        return self.effective_start or self.occurrence_start

    @property
    def ends_at(self) -> datetime | None:
        return self.effective_end if self.effective_start is not None else self.occurrence_end

    def transition(
        self,
        status: OccurrenceStatus,
        *,
        now: datetime,
        result_note: str | None = None,
    ) -> TaskOccurrence:
        return replace(
            self,
            status=status,
            completed_at=now if status is OccurrenceStatus.COMPLETED else None,
            result_note=self.result_note if result_note is None else result_note.strip(),
        )

    def update_result_note(self, result_note: str) -> TaskOccurrence:
        return replace(self, result_note=result_note.strip())
