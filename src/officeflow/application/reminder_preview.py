"""Read-only schedule projection: never polls, claims or consumes a reminder."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from time import monotonic
from typing import Protocol
from zoneinfo import ZoneInfo

from officeflow.application.reminder_runtime import scope_label
from officeflow.application.reminders import ReminderService, ReminderTarget
from officeflow.domain.enums import OccurrenceStatus, ReminderDeliveryStatus
from officeflow.domain.reminder import ReminderDelivery
from officeflow.domain.task import Task


@dataclass(frozen=True, slots=True)
class ReminderPreviewItem:
    task: Task
    reminder_id: int
    occurrence_start: datetime | None
    scheduled_at: datetime
    effective_at: datetime
    status: str
    delivery_id: int | None = None
    snoozed_until: datetime | None = None
    actual_fired_at: datetime | None = None
    occurrence_status: OccurrenceStatus | None = None
    moved_history: bool = False

    @property
    def key(self) -> tuple[int, datetime | None, datetime]:
        return self.reminder_id, self.occurrence_start, self.scheduled_at

    @property
    def is_pending(self) -> bool:
        return (
            not scope_label(self.task, self.occurrence_status)
            and not self.moved_history
            and self.status in {"예정", "미확인", "재알림"}
        )

    @property
    def needs_attention(self) -> bool:
        return (
            not scope_label(self.task, self.occurrence_status)
            and not self.moved_history
            and self.status == "시각 지남 · 발생 기록 없음"
        )

    @property
    def display_status(self) -> str:
        scope = scope_label(self.task, self.occurrence_status)
        state = {"확인함": "알림 확인함", "완료": "알림 처리됨", "대기 처리": "알림 대기 처리"}.get(
            self.status, self.status
        )
        return f"{scope} · {state}" if scope else self.status


@dataclass(frozen=True, slots=True)
class ReminderPreviewResult:
    items: tuple[ReminderPreviewItem, ...]
    complete: bool

    def for_date(self, day: date, timezone: str) -> tuple[ReminderPreviewItem, ...]:
        zone = ZoneInfo(timezone)
        result = []
        for item in self.items:
            if item.effective_at.astimezone(zone).date() == day:
                result.append(item)
            elif (item.snoozed_until or item.actual_fired_at) and item.scheduled_at.astimezone(
                zone
            ).date() == day:
                result.append(
                    replace(
                        item,
                        effective_at=item.scheduled_at,
                        status="다른 날로 미룸"
                        if item.snoozed_until
                        else "다른 날 발생 · " + item.status,
                        moved_history=True,
                    )
                )
        return tuple(sorted(result, key=lambda i: (i.effective_at, i.reminder_id)))


class PreviewRepository(Protocol):
    def preview_targets(
        self, start: datetime, end: datetime, *, after_id: int, limit: int
    ) -> tuple[ReminderTarget, ...]: ...
    def preview_deliveries(
        self, start: datetime, end: datetime, *, limit: int
    ) -> tuple[tuple[ReminderDelivery, ReminderTarget], ...]: ...
    def closed_occurrences(
        self, task_ids: tuple[int, ...], start: datetime, end: datetime
    ) -> frozenset[tuple[int, datetime]]: ...

    def occurrence_states(
        self, keys: tuple[tuple[int, datetime], ...]
    ) -> dict[tuple[int, datetime], OccurrenceStatus]: ...


class ReminderPreviewService:
    def __init__(self, repository: PreviewRepository, reminders: ReminderService) -> None:
        self._repository = repository
        self._reminders = reminders

    def query(
        self,
        date_from: date,
        date_to: date,
        *,
        timezone: str,
        now: datetime | None = None,
        limit: int = 5000,
        seconds: float = 2.0,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> ReminderPreviewResult:
        snapshot = getattr(self._repository, "read_snapshot", None)
        if snapshot:
            with snapshot() as repository:
                return ReminderPreviewService(repository, self._reminders)._query(
                    date_from,
                    date_to,
                    timezone=timezone,
                    now=now,
                    limit=limit,
                    seconds=seconds,
                    cancel_requested=cancel_requested,
                )
        return self._query(
            date_from,
            date_to,
            timezone=timezone,
            now=now,
            limit=limit,
            seconds=seconds,
            cancel_requested=cancel_requested,
        )

    def _query(
        self,
        date_from: date,
        date_to: date,
        *,
        timezone: str,
        now: datetime | None = None,
        limit: int = 5000,
        seconds: float = 2.0,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> ReminderPreviewResult:
        if date_to < date_from or (date_to - date_from).days > 41 or limit < 1 or seconds <= 0:
            raise ValueError("미리보기는 1~42일 범위와 양수 조회 예산이 필요합니다.")
        current = now or datetime.now(UTC)
        zone = ZoneInfo(timezone)
        today = current.astimezone(zone).date()
        start = datetime.combine(date_from, time.min, zone).astimezone(UTC)
        end = datetime.combine(date_to + timedelta(days=1), time.min, zone).astimezone(UTC)
        projection_start = max(start, datetime.combine(today, time.min, zone).astimezone(UTC))
        items: dict[tuple[int, datetime | None, datetime], ReminderPreviewItem] = {}
        began = monotonic()
        complete = True
        cursor = 0

        def exhausted() -> bool:
            return monotonic() - began > seconds or bool(cancel_requested and cancel_requested())

        if projection_start < end:
            while True:
                targets = self._repository.preview_targets(
                    projection_start, end, after_id=cursor, limit=200
                )
                closed = self._repository.closed_occurrences(
                    tuple({t.task.id for t in targets if t.task.id is not None}),
                    projection_start - timedelta(days=32),
                    end + timedelta(days=32),
                )
                for target in targets:
                    assert target.reminder.id is not None
                    cursor = target.reminder.id
                    after = projection_start
                    inclusive = True
                    while True:
                        if len(items) >= limit or exhausted():
                            complete = False
                            break
                        candidate = self._reminders._effective_candidate(
                            target, after=after, inclusive=inclusive
                        )
                        if candidate is None or candidate[1] >= end:
                            break
                        occurrence, scheduled = candidate
                        if (target.task.id, occurrence) not in closed:
                            item = ReminderPreviewItem(
                                target.task,
                                target.reminder.id,
                                occurrence,
                                scheduled,
                                scheduled,
                                "예정"
                                if scheduled >= current.replace(second=0, microsecond=0)
                                else "시각 지남 · 발생 기록 없음",
                            )
                            items[item.key] = item
                        after, inclusive = scheduled, False
                    if not complete:
                        break
                if not complete or len(targets) < 200:
                    break
        deliveries = self._repository.preview_deliveries(start, end, limit=limit + 1)
        scopes = self._repository.occurrence_states(
            tuple(
                {
                    (d.task_id, d.occurrence_start)
                    for d, _t in deliveries[:limit]
                    if d.occurrence_start is not None
                }
            )
        )
        if len(deliveries) > limit:
            complete = False
        for delivery, target in deliveries[:limit]:
            scheduled = delivery.scheduled_at
            effective = delivery.snoozed_until or delivery.last_fired_at
            states = {
                ReminderDeliveryStatus.FIRED: "미확인",
                ReminderDeliveryStatus.SNOOZED: "재알림",
                ReminderDeliveryStatus.ACKNOWLEDGED: "확인함",
                ReminderDeliveryStatus.COMPLETED: "완료",
                ReminderDeliveryStatus.DEFERRED: "대기 처리",
            }
            status = states[delivery.status]
            if delivery.status is ReminderDeliveryStatus.SNOOZED and not start <= effective < end:
                status = "다른 날로 미룸"
            # Keep the original planned time unless a snooze/recovery moved its actual appearance.
            display_time = effective
            item = ReminderPreviewItem(
                target.task,
                delivery.reminder_id,
                delivery.occurrence_start,
                scheduled,
                display_time,
                status,
                delivery.id,
                delivery.snoozed_until,
                delivery.last_fired_at,
                occurrence_status=scopes.get((delivery.task_id, delivery.occurrence_start))
                if delivery.occurrence_start
                else None,
                moved_history=status == "다른 날로 미룸",
            )
            items[item.key] = item
        scopes.update(
            self._repository.occurrence_states(
                tuple(
                    {
                        (item.task.id, item.occurrence_start)
                        for item in items.values()
                        if item.task.id is not None and item.occurrence_start is not None
                    }
                )
            )
        )
        projected = []
        for item in items.values():
            occurrence_status = (
                scopes.get((item.task.id, item.occurrence_start))
                if item.task.id is not None and item.occurrence_start is not None
                else None
            )
            item = replace(item, occurrence_status=occurrence_status)
            if item.delivery_id is None and scope_label(item.task, occurrence_status):
                continue  # Closed future scopes are not fabricated delivery history.
            projected.append(item)
        return ReminderPreviewResult(
            tuple(sorted(projected, key=lambda i: (i.effective_at, i.task.id or 0, i.reminder_id))),
            complete,
        )
