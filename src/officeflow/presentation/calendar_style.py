"""Calendar color/status vocabulary; task completion and alarm handling stay distinct."""

from PySide6.QtGui import QColor, QIcon, QPixmap

from officeflow.application.day_display import DayValue
from officeflow.application.reminder_preview import ReminderPreviewItem
from officeflow.application.tasks import ScheduledTask
from officeflow.domain.enums import TaskPriority, TaskStatus
from officeflow.presentation.schedule_help import STATUS_LABELS


def priority_color(priority: TaskPriority) -> str:
    return {
        TaskPriority.NORMAL: "#4778d9",
        TaskPriority.ATTENTION: "#3b8d78",
        TaskPriority.IMPORTANT: "#cf7b2a",
        TaskPriority.URGENT: "#c74a55",
    }[priority]


def calendar_color(value: ScheduledTask) -> str:
    if value.status in {TaskStatus.COMPLETED, TaskStatus.CANCELED, TaskStatus.ARCHIVED}:
        return "#7d899e"
    return priority_color(value.priority)


def priority_label(priority: TaskPriority) -> str:
    return {
        TaskPriority.NORMAL: "보통",
        TaskPriority.ATTENTION: "관심",
        TaskPriority.IMPORTANT: "중요",
        TaskPriority.URGENT: "긴급",
    }[priority]


def day_status(value: DayValue) -> str:
    if isinstance(value, ReminderPreviewItem):
        return "확인 필요" if value.needs_attention else value.display_status
    return STATUS_LABELS[value.status]


def priority_icon(value: ScheduledTask) -> QIcon:
    stripe = QPixmap(5, 16)
    stripe.fill(QColor(calendar_color(value)))
    return QIcon(stripe)
