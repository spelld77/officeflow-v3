from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    ACTIVE = "active"
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELED = "canceled"
    ARCHIVED = "archived"


class TaskPriority(StrEnum):
    NORMAL = "normal"
    ATTENTION = "attention"
    IMPORTANT = "important"
    URGENT = "urgent"


class OccurrenceStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    CANCELED = "canceled"


class ReminderRelation(StrEnum):
    START = "start"
    END = "end"
    ABSOLUTE = "absolute"


class ReminderDeliveryStatus(StrEnum):
    FIRED = "fired"
    SNOOZED = "snoozed"
    ACKNOWLEDGED = "acknowledged"
    COMPLETED = "completed"
    DEFERRED = "deferred"
