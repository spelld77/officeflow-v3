"""Read-only, calendar-day schedule hints shared by lists, calendar and task viewers."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from officeflow.domain.enums import TaskStatus
from officeflow.domain.task import Task
from officeflow.presentation.dates import format_date, format_datetime

STATUS_LABELS = {
    TaskStatus.ACTIVE: "진행",
    TaskStatus.PENDING: "대기",
    TaskStatus.COMPLETED: "완료",
    TaskStatus.CANCELED: "취소",
    TaskStatus.ARCHIVED: "보관",
}


def schedule_help(task: Task, now: datetime, *, confirmed: bool = True) -> str:
    if now.tzinfo is None:
        raise ValueError("현재 시각에는 시간대 정보가 필요합니다.")
    if task.starts_at is None:
        return "일정 없음"
    zone = ZoneInfo(task.timezone)
    start = task.starts_at.astimezone(zone)
    end = task.ends_at.astimezone(zone) if task.ends_at else None
    local_now = now.astimezone(zone)
    end_day = (
        (end - timedelta(microseconds=1)).date()
        if end and task.all_day
        else (end.date() if end else None)
    )
    if task.all_day:
        text = f"시작: {format_date(start.date())} · 종일"
        if end_day is not None and end_day != start.date():
            text += f"\n종료: {format_date(end_day)} · 종일"
    else:
        text = f"시작: {format_datetime(start, task.timezone)}"
        if end:
            text += f"\n종료: {format_datetime(end, task.timezone)}"
    if not confirmed:
        return text + "\n회차 일정 확인 필요"
    if task.deleted_at is not None:
        return text + "\n휴지통 · 읽기 전용"
    if task.status not in (TaskStatus.ACTIVE, TaskStatus.PENDING):
        return text + f"\n상태: {STATUS_LABELS[task.status]}"
    today = local_now.date()
    if local_now < start:
        days = (start.date() - today).days
        relative = f"시작일은 {days}일 후입니다." if days else "오늘 시작 예정입니다."
    elif end is not None and end_day is not None:
        if local_now < end:
            days = (end_day - today).days
            relative = f"종료일은 {days}일 후입니다." if days > 0 else "오늘 종료 예정입니다."
        else:
            days = (today - end_day).days
            relative = (
                f"종료일로부터 {days}일이 지났습니다."
                if days > 0
                else "종료 예정 시각이 지났습니다."
            )
    else:
        days = (today - start.date()).days
        relative = (
            f"시작일로부터 {days}일이 지났습니다." if days else "오늘 시작 예정 시각이 지났습니다."
        )
    return text + "\n" + relative
