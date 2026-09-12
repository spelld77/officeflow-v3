from __future__ import annotations

from datetime import timedelta
from typing import ClassVar
from zoneinfo import ZoneInfo

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPersistentModelIndex,
    QRectF,
    QSize,
    Qt,
)
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from officeflow.domain.enums import TaskPriority, TaskStatus
from officeflow.domain.task import Task


class TaskListModel(QAbstractListModel):
    TASK_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self) -> None:
        super().__init__()
        self._tasks: list[Task] = []

    def rowCount(
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> int:
        return 0 if parent.isValid() else len(self._tasks)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._tasks):
            return None
        task = self._tasks[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return task.title
        if role == Qt.ItemDataRole.ToolTipRole:
            return task.description or task.title
        if role == self.TASK_ROLE:
            return task
        return None

    def set_tasks(self, tasks: list[Task]) -> None:
        self.beginResetModel()
        self._tasks = list(tasks)
        self.endResetModel()

    def task_at(self, index: QModelIndex) -> Task | None:
        if not index.isValid() or not 0 <= index.row() < len(self._tasks):
            return None
        return self._tasks[index.row()]

    def index_for_task(self, task_id: int) -> QModelIndex:
        for row, task in enumerate(self._tasks):
            if task.id == task_id:
                return self.index(row, 0)
        return QModelIndex()


class TaskItemDelegate(QStyledItemDelegate):
    PRIORITY_COLORS: ClassVar[dict[TaskPriority, QColor]] = {
        TaskPriority.NORMAL: QColor("#94A3B8"),
        TaskPriority.ATTENTION: QColor("#2F6FED"),
        TaskPriority.IMPORTANT: QColor("#7C3AED"),
        TaskPriority.URGENT: QColor("#DC2626"),
    }
    STATUS_LABELS: ClassVar[dict[TaskStatus, str]] = {
        TaskStatus.ACTIVE: "진행",
        TaskStatus.PENDING: "대기",
        TaskStatus.COMPLETED: "완료",
        TaskStatus.CANCELED: "취소",
        TaskStatus.ARCHIVED: "보관",
    }

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        task = index.data(TaskListModel.TASK_ROLE)
        if not isinstance(task, Task):
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect.adjusted(4, 3, -4, -3))
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        background = QColor("#EAF1FF") if selected else QColor("#F8FAFD" if hovered else "#FFFFFF")
        border = QColor("#BFD0F7") if selected else QColor("#E5EAF2")
        painter.setPen(border)
        painter.setBrush(background)
        painter.drawRoundedRect(rect, 9, 9)

        marker = self.PRIORITY_COLORS[task.priority]
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(marker)
        painter.drawRoundedRect(
            QRectF(rect.left() + 10, rect.top() + 13, 5, rect.height() - 26), 2, 2
        )

        text_left = int(rect.left() + 27)
        title_rect = option.rect.adjusted(text_left - option.rect.left(), 10, -92, -31)
        title_font = QFont(option.font)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor("#172033" if task.status is not TaskStatus.COMPLETED else "#778197"))
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            option.fontMetrics.elidedText(
                task.title, Qt.TextElideMode.ElideRight, title_rect.width()
            ),
        )

        meta_rect = option.rect.adjusted(text_left - option.rect.left(), 34, -18, -8)
        meta_font = QFont(option.font)
        meta_font.setPointSize(max(8, option.font.pointSize() - 1))
        painter.setFont(meta_font)
        painter.setPen(QColor("#68738A"))
        painter.drawText(
            meta_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            format_task_schedule(task),
        )

        status_rect = QRectF(rect.right() - 72, rect.top() + 12, 60, 24)
        painter.setBrush(QColor("#EEF2F7"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(status_rect, 12, 12)
        painter.setPen(QColor("#526078"))
        painter.drawText(status_rect, Qt.AlignmentFlag.AlignCenter, self.STATUS_LABELS[task.status])
        painter.restore()

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return QSize(option.rect.width(), 70)


def format_task_schedule(task: Task) -> str:
    if task.starts_at is None:
        return "일정 없음"
    zone = ZoneInfo(task.timezone)
    start = task.starts_at.astimezone(zone)
    if task.all_day:
        if task.ends_at is None:
            return start.strftime("%Y.%m.%d · 종일")
        inclusive_end = (task.ends_at.astimezone(zone) - timedelta(microseconds=1)).date()
        if inclusive_end == start.date():
            return start.strftime("%Y.%m.%d · 종일")
        return f"{start:%Y.%m.%d}부터 {inclusive_end:%Y.%m.%d}까지 · 종일"
    if task.ends_at is None:
        return start.strftime("%Y.%m.%d %H:%M")
    end = task.ends_at.astimezone(zone)
    if start.date() == end.date():
        return f"{start:%Y.%m.%d %H:%M}부터 {end:%H:%M}까지"
    return f"{start:%Y.%m.%d %H:%M}부터 {end:%Y.%m.%d %H:%M}까지"
