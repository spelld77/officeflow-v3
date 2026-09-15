from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from officeflow.application.exporting import ExportCanceledError
from officeflow.domain.enums import TaskStatus
from officeflow.domain.task import Task


class ICalendarTaskExporter:
    def export(
        self,
        tasks: tuple[Task, ...],
        destination: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        target = destination.resolve()
        if target.suffix.casefold() != ".ics":
            raise ValueError("캘린더 내보내기 파일은 .ics 확장자여야 합니다.")
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "BEGIN:VCALENDAR",
            "PRODID:-//OfficeFlow//OfficeFlow v3//KO",
            "VERSION:2.0",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "X-WR-CALNAME:OfficeFlow",
        ]
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        for task in tasks:
            if cancel_requested is not None and cancel_requested():
                raise ExportCanceledError("캘린더 내보내기를 취소했습니다.")
            lines.extend(self._event_lines(task, timestamp))
        lines.append("END:VCALENDAR")
        payload = "\r\n".join(_fold_line(line) for line in lines) + "\r\n"

        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{target.stem}-", suffix=".part.ics", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if cancel_requested is not None and cancel_requested():
                raise ExportCanceledError("캘린더 내보내기를 취소했습니다.")
            os.replace(temporary, target)
            return target
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _event_lines(task: Task, timestamp: str) -> list[str]:
        if task.starts_at is None:
            return []
        task_id = task.id if task.id is not None else f"unsaved-{task.created_at.timestamp()}"
        lines = [
            "BEGIN:VEVENT",
            f"UID:task-{task_id}@officeflow.local",
            f"DTSTAMP:{timestamp}",
            f"CREATED:{task.created_at.astimezone(UTC):%Y%m%dT%H%M%SZ}",
            f"LAST-MODIFIED:{task.updated_at.astimezone(UTC):%Y%m%dT%H%M%SZ}",
            f"SUMMARY:{_escape_text(task.title)}",
        ]
        zone = ZoneInfo(task.timezone)
        start = task.starts_at.astimezone(zone)
        end = task.ends_at.astimezone(zone) if task.ends_at is not None else None
        if task.all_day:
            end_date = end.date() if end is not None else start.date() + timedelta(days=1)
            lines.extend(
                (
                    f"DTSTART;VALUE=DATE:{start:%Y%m%d}",
                    f"DTEND;VALUE=DATE:{end_date:%Y%m%d}",
                )
            )
        else:
            lines.append(f"DTSTART;TZID={task.timezone}:{start:%Y%m%dT%H%M%S}")
            if end is not None:
                lines.append(f"DTEND;TZID={task.timezone}:{end:%Y%m%dT%H%M%S}")
        if task.recurrence_rule:
            lines.append(f"RRULE:{task.recurrence_rule}")
        if task.description:
            lines.append(f"DESCRIPTION:{_escape_text(task.description)}")
        if task.result_note:
            lines.append(f"X-OFFICEFLOW-RESULT:{_escape_text(task.result_note)}")
        lines.append(f"STATUS:{_calendar_status(task.status)}")
        lines.append("END:VEVENT")
        return lines


def _calendar_status(status: TaskStatus) -> str:
    if status is TaskStatus.COMPLETED:
        return "COMPLETED"
    if status in {TaskStatus.CANCELED, TaskStatus.ARCHIVED}:
        return "CANCELLED"
    if status is TaskStatus.PENDING:
        return "TENTATIVE"
    return "CONFIRMED"


def _escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold_line(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= 75:
        return value
    parts: list[str] = []
    remaining = encoded
    limit = 75
    while remaining:
        cut = min(limit, len(remaining))
        while cut > 0:
            try:
                part = remaining[:cut].decode("utf-8")
                break
            except UnicodeDecodeError:
                cut -= 1
        if cut == 0:
            raise UnicodeError("iCalendar 줄을 UTF-8 경계에서 나눌 수 없습니다.")
        parts.append(part)
        remaining = remaining[cut:]
        limit = 74
    return "\r\n ".join(parts)
