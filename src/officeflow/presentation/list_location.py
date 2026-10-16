"""Small, process-local navigation bookmarks; never cache task text or files."""

from dataclasses import dataclass
from datetime import date, datetime

from officeflow.application.tasks import TaskGroup


@dataclass(frozen=True, slots=True)
class ListLocation:
    query: tuple[object, ...]
    day: date
    selected: tuple[int, datetime | None] | None
    top: tuple[int, datetime | None] | None
    inset: int
    scroll: int
    loaded: int
    groups: dict[TaskGroup, int]
    truncated: bool = False
