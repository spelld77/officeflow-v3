"""Manual immutable task states. A command never silently drops selected files."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True, order=True)
class SnapshotSelection:
    kind: str
    id: int

    def __post_init__(self) -> None:
        if self.kind not in {"attachment", "version"} or type(self.id) is not int or self.id < 1:
            raise ValueError("같은 업무의 첨부/보관본을 선택하세요.")


@dataclass(frozen=True, slots=True)
class SnapshotCommand:
    job_id: str
    task_id: int
    label: str
    selections: tuple[SnapshotSelection, ...] = ()
    occurrence_start: datetime | None = None
    documents_closed: bool = False

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[0-9a-f]{32}", self.job_id)
            or type(self.task_id) is not int
            or self.task_id < 1
        ):
            raise ValueError("올바른 업무와 보관 식별자가 필요합니다.")
        if not self.label.strip() or len(self.label.strip()) > 200:
            raise ValueError("보관 이름은 1~200자입니다.")
        if len(self.selections) > 100 or len(set(self.selections)) != len(self.selections):
            raise ValueError("파일은 중복 없이 최대 100개 선택합니다.")
        if self.selections and not self.documents_closed:
            raise ValueError("선택 문서를 저장하고 닫았는지 확인하세요.")
        if self.occurrence_start is not None and self.occurrence_start.tzinfo is None:
            raise ValueError("회차 시각에는 시간대가 필요합니다.")
        object.__setattr__(self, "label", self.label.strip())
        object.__setattr__(self, "selections", tuple(sorted(self.selections)))


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    job_id: str
    snapshot_id: int
    already_committed: bool = False
    warning: str = ""


@dataclass(frozen=True, slots=True)
class SnapshotDetail:
    id: int
    task_id: int
    label: str
    captured_at: datetime
    committed_at: datetime
    occurrence_start: datetime | None
    info: dict[str, Any]
    file_count: int
