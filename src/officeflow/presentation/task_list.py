from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import ClassVar
from zoneinfo import ZoneInfo

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from officeflow.application.tasks import TaskGroup, TaskPage
from officeflow.domain.enums import TaskPriority, TaskStatus
from officeflow.domain.task import Task

PageLoader = Callable[[int, int], TaskPage]
GroupPageLoader = Callable[[TaskGroup, int, int], TaskPage]


@dataclass(frozen=True, slots=True)
class GroupHeader:
    group: TaskGroup
    label: str
    total: int
    collapsed: bool


@dataclass(frozen=True, slots=True)
class LoadMoreRow:
    group: TaskGroup
    remaining: int


@dataclass(slots=True)
class _GroupState:
    total: int
    items: list[Task]
    collapsed: bool


class TaskListModel(QAbstractListModel):
    TASK_ROLE = Qt.ItemDataRole.UserRole + 1
    ENTRY_ROLE = Qt.ItemDataRole.UserRole + 2
    PAGE_SIZE = 50
    GROUP_LABELS: ClassVar[dict[TaskGroup, str]] = {
        TaskGroup.OVERDUE: "처리 필요",
        TaskGroup.IN_PROGRESS: "오늘 할 일",
        TaskGroup.UPCOMING: "오늘 할 일",
        TaskGroup.COMPLETED: "완료한 업무",
    }

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[Task | GroupHeader | LoadMoreRow] = []
        self._flat_items: list[Task] = []
        self._flat_total = 0
        self._flat_loader: PageLoader | None = None
        self._groups: dict[TaskGroup, _GroupState] = {}
        self._group_loader: GroupPageLoader | None = None
        self._grouped = False

    def rowCount(
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        entry = self._rows[index.row()]
        if role == self.ENTRY_ROLE:
            return entry
        if isinstance(entry, Task):
            if role == Qt.ItemDataRole.DisplayRole:
                return entry.title
            if role == Qt.ItemDataRole.ToolTipRole:
                attachment = "\n첨부파일 있음" if entry.has_attachments else ""
                return f"{entry.description or entry.title}{attachment}"
            if role == self.TASK_ROLE:
                return entry
        elif isinstance(entry, GroupHeader) and role == Qt.ItemDataRole.DisplayRole:
            return f"{entry.label} ({entry.total})"
        elif isinstance(entry, LoadMoreRow) and role == Qt.ItemDataRole.DisplayRole:
            return f"{entry.remaining}개 더 보기"
        return None

    def set_tasks(self, tasks: list[Task]) -> None:
        self.set_page(TaskPage(tuple(tasks), len(tasks), 0, None))

    def set_page(self, page: TaskPage, loader: PageLoader | None = None) -> None:
        self.beginResetModel()
        self._grouped = False
        self._groups.clear()
        self._group_loader = None
        self._flat_items = list(page.items)
        self._flat_total = page.total
        self._flat_loader = loader
        self._rows = list(self._flat_items)
        self.endResetModel()

    def set_group_pages(
        self,
        pages: Mapping[TaskGroup, TaskPage],
        *,
        collapsed: frozenset[TaskGroup],
        loader: GroupPageLoader,
    ) -> None:
        self.beginResetModel()
        self._grouped = True
        self._flat_items.clear()
        self._flat_total = 0
        self._flat_loader = None
        self._group_loader = loader
        self._groups = {
            group: _GroupState(
                total=pages[group].total,
                items=list(pages[group].items),
                collapsed=group in collapsed,
            )
            for group in pages
        }
        self._rebuild_group_rows()
        self.endResetModel()

    def canFetchMore(
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> bool:
        return (
            not parent.isValid()
            and not self._grouped
            and self._flat_loader is not None
            and len(self._flat_items) < self._flat_total
        )

    def fetchMore(
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> None:
        if parent.isValid() or not self.canFetchMore() or self._flat_loader is None:
            return
        page = self._flat_loader(len(self._flat_items), self.PAGE_SIZE)
        if not page.items:
            self._flat_total = len(self._flat_items)
            return
        first = len(self._rows)
        last = first + len(page.items) - 1
        self.beginInsertRows(QModelIndex(), first, last)
        self._flat_items.extend(page.items)
        self._rows.extend(page.items)
        self._flat_total = page.total
        self.endInsertRows()

    def toggle_group(self, group: TaskGroup) -> bool:
        state = self._groups.get(group)
        if state is None:
            return False
        self.beginResetModel()
        state.collapsed = not state.collapsed
        self._rebuild_group_rows()
        self.endResetModel()
        return state.collapsed

    def expand_group(self, group: TaskGroup) -> None:
        state = self._groups.get(group)
        if state is not None and state.collapsed:
            self.toggle_group(group)

    def load_more(self, group: TaskGroup) -> None:
        state = self._groups.get(group)
        if state is None or self._group_loader is None or len(state.items) >= state.total:
            return
        page = self._group_loader(group, len(state.items), self.PAGE_SIZE)
        self.beginResetModel()
        state.items.extend(page.items)
        state.total = page.total
        self._rebuild_group_rows()
        self.endResetModel()

    def entry_at(self, index: QModelIndex) -> Task | GroupHeader | LoadMoreRow | None:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        return self._rows[index.row()]

    def task_at(self, index: QModelIndex) -> Task | None:
        entry = self.entry_at(index)
        return entry if isinstance(entry, Task) else None

    def index_for_task(self, task_id: int) -> QModelIndex:
        for row, entry in enumerate(self._rows):
            if isinstance(entry, Task) and entry.id == task_id:
                return self.index(row, 0)
        return QModelIndex()

    def index_for_group(self, group: TaskGroup) -> QModelIndex:
        for row, entry in enumerate(self._rows):
            if isinstance(entry, GroupHeader) and entry.group is group:
                return self.index(row, 0)
        return QModelIndex()

    @property
    def loaded_task_count(self) -> int:
        if not self._grouped:
            return len(self._flat_items)
        return sum(len(state.items) for state in self._groups.values())

    @property
    def total_task_count(self) -> int:
        if not self._grouped:
            return self._flat_total
        return sum(state.total for state in self._groups.values())

    def _rebuild_group_rows(self) -> None:
        rows: list[Task | GroupHeader | LoadMoreRow] = []
        for group, state in self._groups.items():
            if state.total == 0:
                continue
            rows.append(
                GroupHeader(
                    group=group,
                    label=self.GROUP_LABELS[group],
                    total=state.total,
                    collapsed=state.collapsed,
                )
            )
            if state.collapsed:
                continue
            rows.extend(state.items)
            remaining = state.total - len(state.items)
            if remaining > 0:
                rows.append(LoadMoreRow(group=group, remaining=remaining))
        self._rows = rows


class TaskItemDelegate(QStyledItemDelegate):
    quickCompleteRequested = Signal(object)
    menuRequested = Signal(object, object)

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

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._compact = False

    def set_compact(self, compact: bool) -> None:
        self._compact = compact

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        entry = index.data(TaskListModel.ENTRY_ROLE)
        if isinstance(entry, GroupHeader):
            self._paint_group_header(painter, option, entry)
            return
        if isinstance(entry, LoadMoreRow):
            self._paint_load_more(painter, option, entry)
            return
        if not isinstance(entry, Task):
            super().paint(painter, option, index)
            return
        self._paint_task(painter, option, entry)

    def _paint_group_header(
        self, painter: QPainter, option: QStyleOptionViewItem, header: GroupHeader
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect.adjusted(4, 3, -4, -3))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#EEF3FA"))
        painter.drawRoundedRect(rect, 8, 8)
        font = QFont(option.font)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#27344A"))
        chevron = "▶" if header.collapsed else "▼"
        painter.drawText(
            rect.adjusted(12, 0, -12, 0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            f"{chevron}  {header.label}  {header.total}",
        )
        painter.restore()

    def _paint_load_more(
        self, painter: QPainter, option: QStyleOptionViewItem, row: LoadMoreRow
    ) -> None:
        painter.save()
        painter.setPen(QColor("#2F6FED"))
        painter.drawText(
            option.rect,
            Qt.AlignmentFlag.AlignCenter,
            f"{row.remaining}개 더 보기",
        )
        painter.restore()

    def _paint_task(self, painter: QPainter, option: QStyleOptionViewItem, task: Task) -> None:
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

        completion_rect = self._completion_rect(option)
        completed = task.status is TaskStatus.COMPLETED
        painter.setPen(QPen(QColor("#2F6FED" if completed else "#A8B3C5"), 1.6))
        painter.setBrush(QColor("#2F6FED") if completed else QColor("#FFFFFF"))
        painter.drawEllipse(completion_rect)
        if completed:
            painter.setPen(QPen(QColor("#FFFFFF"), 1.8))
            painter.drawLine(
                QPoint(int(completion_rect.left() + 4), int(completion_rect.center().y())),
                QPoint(int(completion_rect.left() + 8), int(completion_rect.bottom() - 4)),
            )
            painter.drawLine(
                QPoint(int(completion_rect.left() + 8), int(completion_rect.bottom() - 4)),
                QPoint(int(completion_rect.right() - 3), int(completion_rect.top() + 4)),
            )

        marker = self.PRIORITY_COLORS[task.priority]
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(marker)
        painter.drawRoundedRect(
            QRectF(rect.left() + 40, rect.top() + 10, 4, rect.height() - 20), 2, 2
        )

        text_left = int(rect.left() + 54)
        title_bottom = -8 if self._compact else -31
        title_right_margin = -178 if task.has_attachments else -124
        title_rect = option.rect.adjusted(
            text_left - option.rect.left(), 8, title_right_margin, title_bottom
        )
        title_font = QFont(option.font)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor("#172033" if task.status is not TaskStatus.COMPLETED else "#778197"))
        title = (
            f"{format_task_schedule_compact(task)}  ·  {task.title}"
            if self._compact
            else task.title
        )
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            option.fontMetrics.elidedText(
                title, Qt.TextElideMode.ElideRight, title_rect.width()
            ),
        )

        if not self._compact:
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

        status_top = rect.top() + (7 if self._compact else 12)
        if task.has_attachments:
            attachment_rect = QRectF(rect.right() - 158, status_top, 48, 24)
            painter.setBrush(QColor("#EAF1FF"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(attachment_rect, 12, 12)
            painter.setPen(QColor("#2F6FED"))
            painter.drawText(attachment_rect, Qt.AlignmentFlag.AlignCenter, "첨부")
        status_rect = QRectF(rect.right() - 104, status_top, 60, 24)
        painter.setBrush(QColor("#EEF2F7"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(status_rect, 12, 12)
        painter.setPen(QColor("#526078"))
        painter.drawText(status_rect, Qt.AlignmentFlag.AlignCenter, self.STATUS_LABELS[task.status])
        painter.setPen(QColor("#68738A"))
        painter.drawText(
            self._menu_rect(option),
            Qt.AlignmentFlag.AlignCenter,
            "⋯",
        )
        painter.restore()

    def editorEvent(
        self,
        event: QEvent,
        model: QAbstractItemModel,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        entry = index.data(TaskListModel.ENTRY_ROLE)
        if (
            not isinstance(entry, Task)
            or event.type() is not QEvent.Type.MouseButtonRelease
            or not isinstance(event, QMouseEvent)
            or event.button() is not Qt.MouseButton.LeftButton
        ):
            return super().editorEvent(event, model, option, index)
        if self._menu_rect(option).contains(event.position()):
            self.menuRequested.emit(entry, event.globalPosition().toPoint())
            return True
        if (
            entry.status in {TaskStatus.ACTIVE, TaskStatus.PENDING}
            and self._completion_rect(option).contains(event.position())
        ):
            self.quickCompleteRequested.emit(entry)
            return True
        return super().editorEvent(event, model, option, index)

    @staticmethod
    def _completion_rect(option: QStyleOptionViewItem) -> QRectF:
        center_y = option.rect.center().y()
        return QRectF(option.rect.left() + 16, center_y - 9, 18, 18)

    @staticmethod
    def _menu_rect(option: QStyleOptionViewItem) -> QRectF:
        return QRectF(option.rect.right() - 38, option.rect.top(), 34, option.rect.height())

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        entry = index.data(TaskListModel.ENTRY_ROLE)
        if isinstance(entry, GroupHeader):
            return QSize(option.rect.width(), 42)
        if isinstance(entry, LoadMoreRow):
            return QSize(option.rect.width(), 38)
        return QSize(option.rect.width(), 48 if self._compact else 70)


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


def format_task_schedule_compact(task: Task) -> str:
    if task.starts_at is None:
        return "일정 없음"
    zone = ZoneInfo(task.timezone)
    start = task.starts_at.astimezone(zone)
    today = datetime.now(zone).date()
    if task.all_day:
        return "종일" if start.date() == today else start.strftime("%m.%d 종일")
    return start.strftime("%H:%M" if start.date() == today else "%m.%d %H:%M")
