from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from officeflow.domain.enums import ReminderDeliveryStatus, ReminderRelation


class ReminderValidationError(ValueError):
    """Raised when a reminder rule or delivery violates an invariant."""


def _require_aware(value: datetime | None, label: str) -> None:
    if value is not None and value.tzinfo is None:
        raise ReminderValidationError(f"{label}에 시간대 정보가 필요합니다.")


@dataclass(frozen=True, slots=True)
class ReminderRuleInput:
    relation: ReminderRelation
    offset_minutes: int | None = None
    absolute_at: datetime | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        _require_aware(self.absolute_at, "절대 알림 시각")
        if self.relation is ReminderRelation.ABSOLUTE:
            if self.absolute_at is None or self.offset_minutes is not None:
                raise ReminderValidationError("절대 알림에는 알림 시각만 지정해야 합니다.")
        elif self.offset_minutes is None or self.absolute_at is not None:
            raise ReminderValidationError("시작·종료 알림에는 기준 시각과의 차이가 필요합니다.")
        if self.offset_minutes is not None and not -43_200 <= self.offset_minutes <= 43_200:
            raise ReminderValidationError("알림 간격은 기준 시각 전후 30일 이내여야 합니다.")

    @property
    def identity(self) -> tuple[ReminderRelation, int | None, datetime | None]:
        return self.relation, self.offset_minutes, self.absolute_at


@dataclass(frozen=True, slots=True)
class Reminder:
    id: int | None
    task_id: int
    relation: ReminderRelation
    offset_minutes: int | None
    absolute_at: datetime | None
    enabled: bool
    last_fired_key: str | None = None

    def __post_init__(self) -> None:
        ReminderRuleInput(
            relation=self.relation,
            offset_minutes=self.offset_minutes,
            absolute_at=self.absolute_at,
            enabled=self.enabled,
        )

    @property
    def identity(self) -> tuple[ReminderRelation, int | None, datetime | None]:
        return self.relation, self.offset_minutes, self.absolute_at


@dataclass(frozen=True, slots=True)
class ReminderDelivery:
    id: int | None
    reminder_id: int
    task_id: int
    occurrence_start: datetime | None
    scheduled_at: datetime
    fire_key: str
    status: ReminderDeliveryStatus
    first_fired_at: datetime
    last_fired_at: datetime
    snoozed_until: datetime | None
    acknowledged_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for value, label in (
            (self.occurrence_start, "발생 시작 시각"),
            (self.scheduled_at, "예정 알림 시각"),
            (self.first_fired_at, "최초 알림 시각"),
            (self.last_fired_at, "최근 알림 시각"),
            (self.snoozed_until, "다시 알림 시각"),
            (self.acknowledged_at, "확인 시각"),
            (self.created_at, "생성 시각"),
            (self.updated_at, "수정 시각"),
        ):
            _require_aware(value, label)
        if not self.fire_key or len(self.fire_key) > 200:
            raise ReminderValidationError("알림 중복 방지 키가 올바르지 않습니다.")
        if self.status is ReminderDeliveryStatus.SNOOZED and self.snoozed_until is None:
            raise ReminderValidationError("다시 알림 상태에는 다음 알림 시각이 필요합니다.")

    @classmethod
    def fired(
        cls,
        *,
        reminder_id: int,
        task_id: int,
        occurrence_start: datetime | None,
        scheduled_at: datetime,
        fire_key: str,
        now: datetime,
    ) -> ReminderDelivery:
        return cls(
            id=None,
            reminder_id=reminder_id,
            task_id=task_id,
            occurrence_start=occurrence_start,
            scheduled_at=scheduled_at,
            fire_key=fire_key,
            status=ReminderDeliveryStatus.FIRED,
            first_fired_at=now,
            last_fired_at=now,
            snoozed_until=None,
            acknowledged_at=None,
            created_at=now,
            updated_at=now,
        )

    def snooze(self, minutes: int, *, now: datetime) -> ReminderDelivery:
        if not 1 <= minutes <= 1_440:
            raise ReminderValidationError("다시 알림은 1분에서 24시간 사이여야 합니다.")
        if self.status is not ReminderDeliveryStatus.FIRED:
            raise ReminderValidationError("표시 중인 알림만 다시 알림으로 설정할 수 있습니다.")
        return replace(
            self,
            status=ReminderDeliveryStatus.SNOOZED,
            snoozed_until=now + timedelta(minutes=minutes),
            acknowledged_at=None,
            updated_at=now,
        )

    def refire(self, *, now: datetime) -> ReminderDelivery:
        if self.status is not ReminderDeliveryStatus.SNOOZED:
            raise ReminderValidationError("다시 알림 대기 중인 항목만 재알림할 수 있습니다.")
        return replace(
            self,
            status=ReminderDeliveryStatus.FIRED,
            last_fired_at=now,
            snoozed_until=None,
            updated_at=now,
        )

    def acknowledge(
        self,
        status: ReminderDeliveryStatus = ReminderDeliveryStatus.ACKNOWLEDGED,
        *,
        now: datetime | None = None,
    ) -> ReminderDelivery:
        if status not in {
            ReminderDeliveryStatus.ACKNOWLEDGED,
            ReminderDeliveryStatus.COMPLETED,
            ReminderDeliveryStatus.DEFERRED,
        }:
            raise ReminderValidationError("알림을 종료할 수 있는 상태가 아닙니다.")
        if self.status not in {
            ReminderDeliveryStatus.FIRED,
            ReminderDeliveryStatus.SNOOZED,
        }:
            raise ReminderValidationError("이미 처리된 알림입니다.")
        timestamp = now or datetime.now(UTC)
        return replace(
            self,
            status=status,
            snoozed_until=None,
            acknowledged_at=timestamp,
            updated_at=timestamp,
        )
