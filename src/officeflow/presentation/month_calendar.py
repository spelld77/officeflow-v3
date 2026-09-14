from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFocusEvent, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.tasks import ScheduledTask
from officeflow.domain.enums import TaskPriority, TaskStatus

WEEKDAY_LABELS = ("월", "화", "수", "목", "금", "토", "일")
MONTH_LABELS = tuple(f"{month}월" for month in range(1, 13))


@dataclass(frozen=True, slots=True)
class CalendarSegment:
    task: ScheduledTask
    week: int
    start_column: int
    end_column: int
    lane: int
    continues_before: bool
    continues_after: bool


def month_grid_start(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first - timedelta(days=first.weekday())


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    offset = year * 12 + month - 1 + delta
    return divmod(offset, 12)[0], divmod(offset, 12)[1] + 1


def task_date_span(task: ScheduledTask, timezone: str) -> tuple[date, date] | None:
    """Return the inclusive local date span occupied by a scheduled task."""
    if task.starts_at is None:
        return None
    zone = ZoneInfo(timezone)
    start_day = task.starts_at.astimezone(zone).date()
    if task.ends_at is None:
        return start_day, start_day
    inclusive_end = task.ends_at.astimezone(zone) - timedelta(microseconds=1)
    return start_day, max(start_day, inclusive_end.date())


def tasks_for_date(
    tasks: tuple[ScheduledTask, ...], day: date, timezone: str
) -> tuple[ScheduledTask, ...]:
    matching: list[ScheduledTask] = []
    for task in tasks:
        span = task_date_span(task, timezone)
        if span is not None and span[0] <= day <= span[1]:
            matching.append(task)
    return tuple(matching)


def build_calendar_segments(
    tasks: tuple[ScheduledTask, ...],
    grid_start: date,
    timezone: str,
) -> tuple[CalendarSegment, ...]:
    grid_end = grid_start + timedelta(days=41)
    by_week: list[list[tuple[ScheduledTask, date, date, date, date]]] = [[] for _ in range(6)]
    for task in tasks:
        span = task_date_span(task, timezone)
        if span is None or span[1] < grid_start or span[0] > grid_end:
            continue
        visible_start = max(span[0], grid_start)
        visible_end = min(span[1], grid_end)
        first_week = (visible_start - grid_start).days // 7
        last_week = (visible_end - grid_start).days // 7
        for week in range(first_week, last_week + 1):
            week_start = grid_start + timedelta(days=week * 7)
            segment_start = max(visible_start, week_start)
            segment_end = min(visible_end, week_start + timedelta(days=6))
            by_week[week].append((task, segment_start, segment_end, span[0], span[1]))

    result: list[CalendarSegment] = []
    for week, candidates in enumerate(by_week):
        occupied_until: list[int] = []
        candidates.sort(
            key=lambda item: (
                (item[1] - grid_start).days % 7,
                -(item[2] - item[1]).days,
                not item[0].is_pinned,
                item[0].starts_at or datetime.max.replace(tzinfo=ZoneInfo("UTC")),
                item[0].id or 0,
            )
        )
        week_start = grid_start + timedelta(days=week * 7)
        for task, segment_start, segment_end, task_start, task_end in candidates:
            start_column = (segment_start - week_start).days
            end_column = (segment_end - week_start).days
            lane = next(
                (
                    index
                    for index, previous_end in enumerate(occupied_until)
                    if previous_end < start_column
                ),
                len(occupied_until),
            )
            if lane == len(occupied_until):
                occupied_until.append(end_column)
            else:
                occupied_until[lane] = end_column
            result.append(
                CalendarSegment(
                    task=task,
                    week=week,
                    start_column=start_column,
                    end_column=end_column,
                    lane=lane,
                    continues_before=task_start < segment_start,
                    continues_after=task_end > segment_end,
                )
            )
    return tuple(result)


class MonthCalendarWidget(QWidget):
    dateSelected = Signal(object)
    dateActivated = Signal(object)
    taskSelected = Signal(object)
    taskActivated = Signal(object)
    moreRequested = Signal(object)

    HEADER_HEIGHT = 26.0
    DATE_HEIGHT = 23.0
    BAR_HEIGHT = 18.0

    def __init__(self, *, timezone: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        today = datetime.now(ZoneInfo(timezone)).date()
        self._timezone = timezone
        self._year = today.year
        self._month = today.month
        self._selected_date = today
        self._tasks: tuple[ScheduledTask, ...] = ()
        self._compact = False
        self._task_hits: list[tuple[QRectF, ScheduledTask]] = []
        self._more_hits: list[tuple[QRectF, date]] = []
        self.setObjectName("monthCalendar")
        self.setMinimumSize(QSize(460, 285))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName("월간 캘린더")

    @property
    def displayed_month(self) -> tuple[int, int]:
        return self._year, self._month

    @property
    def selected_date(self) -> date:
        return self._selected_date

    @property
    def visible_date_range(self) -> tuple[date, date]:
        start = month_grid_start(self._year, self._month)
        return start, start + timedelta(days=42)

    def set_month(self, year: int, month: int) -> None:
        date(year, month, 1)
        self._year, self._month = year, month
        self.update()

    def set_selected_date(self, selected: date) -> None:
        self._selected_date = selected
        self.update()

    def set_tasks(self, tasks: tuple[ScheduledTask, ...]) -> None:
        self._tasks = tasks
        self.update()

    def set_compact(self, compact: bool) -> None:
        if compact != self._compact:
            self._compact = compact
            self.setMinimumHeight(220 if compact else 285)
            self.updateGeometry()
            self.update()

    def sizeHint(self) -> QSize:
        return QSize(720, 430)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#FFFFFF"))
        self._task_hits.clear()
        self._more_hits.clear()

        column_width = self.width() / 7.0
        row_height = max(1.0, (self.height() - self.HEADER_HEIGHT) / 6.0)
        for column, label in enumerate(WEEKDAY_LABELS):
            color = "#2F6FED" if column == 5 else "#D14343" if column == 6 else "#68738A"
            painter.setPen(QColor(color))
            painter.drawText(
                QRectF(column * column_width, 0, column_width, self.HEADER_HEIGHT),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )

        grid_start = month_grid_start(self._year, self._month)
        today = datetime.now(ZoneInfo(self._timezone)).date()
        for offset in range(42):
            day = grid_start + timedelta(days=offset)
            row, column = divmod(offset, 7)
            cell = QRectF(
                column * column_width,
                self.HEADER_HEIGHT + row * row_height,
                column_width,
                row_height,
            )
            painter.fillRect(
                cell, QColor("#FAFBFD") if day.month != self._month else QColor("#FFFFFF")
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#E5EAF2"), 1))
            painter.drawRect(cell)
            if day == self._selected_date:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor("#2F6FED"), 2))
                painter.drawRect(cell.adjusted(1, 1, -1, -1))
            day_rect = QRectF(cell.left() + 6, cell.top() + 3, 25, 20)
            if day == today:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#2F6FED"))
                painter.drawEllipse(day_rect.adjusted(2, 0, 2, 0))
                painter.setPen(QColor("#FFFFFF"))
            elif day.month != self._month:
                painter.setPen(QColor("#AAB3C2"))
            elif column == 6:
                painter.setPen(QColor("#D14343"))
            elif column == 5:
                painter.setPen(QColor("#2F6FED"))
            else:
                painter.setPen(QColor("#364158"))
            painter.drawText(day_rect, Qt.AlignmentFlag.AlignCenter, str(day.day))

        lane_limit = 2 if self._compact else 3
        available_lanes = max(1, int((row_height - self.DATE_HEIGHT - 22) // self.BAR_HEIGHT))
        lane_limit = min(lane_limit, available_lanes)
        segments = build_calendar_segments(self._tasks, grid_start, self._timezone)
        visible_task_days: set[tuple[int | None, date]] = set()
        for segment in segments:
            if segment.lane >= lane_limit:
                continue
            x = segment.start_column * column_width + 3
            width = (segment.end_column - segment.start_column + 1) * column_width - 6
            y = (
                self.HEADER_HEIGHT
                + segment.week * row_height
                + self.DATE_HEIGHT
                + segment.lane * self.BAR_HEIGHT
            )
            rect = QRectF(x, y, width, self.BAR_HEIGHT - 2)
            task_color = self._task_color(segment.task)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(task_color)
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(QColor("#FFFFFF"))
            prefix = "◀ " if segment.continues_before else ""
            if segment.task.occurrence_start is not None:
                prefix = f"반복 · {prefix}"
            suffix = " ▶" if segment.continues_after else ""
            title = prefix + segment.task.title + suffix
            text_rect = rect.adjusted(5, 0, -5, 0)
            elided = painter.fontMetrics().elidedText(
                title, Qt.TextElideMode.ElideRight, int(text_rect.width())
            )
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, elided)
            self._task_hits.append((rect, segment.task))
            week_start = grid_start + timedelta(days=segment.week * 7)
            for column in range(segment.start_column, segment.end_column + 1):
                visible_task_days.add((segment.task.id, week_start + timedelta(days=column)))

        for offset in range(42):
            day = grid_start + timedelta(days=offset)
            hidden = sum(
                1
                for task in tasks_for_date(self._tasks, day, self._timezone)
                if (task.id, day) not in visible_task_days
            )
            if hidden == 0:
                continue
            row, column = divmod(offset, 7)
            label = f"+{hidden}개 더 보기" if column_width >= 130 else f"+{hidden}개"
            label_width = min(
                column_width - 8,
                painter.fontMetrics().horizontalAdvance(label) + 12,
            )
            rect = QRectF(
                (column + 1) * column_width - label_width - 4,
                self.HEADER_HEIGHT + (row + 1) * row_height - 20,
                label_width,
                18,
            )
            painter.setPen(QPen(QColor("#D9E0EA"), 1))
            painter.setBrush(QColor("#F3F6FA"))
            painter.drawRoundedRect(rect, 6, 6)
            painter.setPen(QColor("#526078"))
            painter.drawText(
                rect,
                Qt.AlignmentFlag.AlignCenter,
                label,
            )
            self._more_hits.append((rect, day))

        if self.hasFocus():
            painter.setPen(QPen(QColor("#2F6FED"), 1, Qt.PenStyle.DashLine))
            painter.drawRect(QRectF(self.rect()).adjusted(1, 1, -2, -2))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() is not Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.setFocus()
        for rect, hit_day in reversed(self._more_hits):
            if rect.contains(event.position()):
                self._selected_date = hit_day
                self.moreRequested.emit(hit_day)
                self.update()
                return
        task = self._task_at(event.position())
        if task is not None:
            clicked_day = self._date_at(event.position())
            if clicked_day is not None:
                self._selected_date = clicked_day
                self.dateSelected.emit(clicked_day)
            self.taskSelected.emit(task)
            self.update()
            return
        selected_day = self._date_at(event.position())
        if selected_day is not None:
            self._selected_date = selected_day
            self.dateSelected.emit(selected_day)
            self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        task = self._task_at(event.position())
        if task is not None:
            self.taskActivated.emit(task)
            return
        day = self._date_at(event.position())
        if day is not None:
            self._selected_date = day
            self.dateActivated.emit(day)
            self.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        movement = {
            int(Qt.Key.Key_Left): -1,
            int(Qt.Key.Key_Right): 1,
            int(Qt.Key.Key_Up): -7,
            int(Qt.Key.Key_Down): 7,
        }.get(event.key())
        if movement is not None:
            self._select_from_keyboard(self._selected_date + timedelta(days=movement))
            return
        if event.key() in {Qt.Key.Key_PageUp, Qt.Key.Key_PageDown}:
            year, month = shift_month(
                self._year, self._month, -1 if event.key() == Qt.Key.Key_PageUp else 1
            )
            target = date(year, month, min(self._selected_date.day, _days_in_month(year, month)))
            self._select_from_keyboard(target)
            return
        if event.key() == Qt.Key.Key_Home:
            self._select_from_keyboard(datetime.now(ZoneInfo(self._timezone)).date())
            return
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self.dateActivated.emit(self._selected_date)
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event: QFocusEvent) -> None:
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        self.update()

    def event(self, event: QEvent) -> bool:
        if event.type() is QEvent.Type.ToolTip:
            task = self._task_at(QPointF(self.mapFromGlobal(self.cursor().pos())))
            self.setToolTip(
                task.title if task is not None else "날짜를 더블 클릭하면 일정을 등록합니다."
            )
        return super().event(event)

    def _select_from_keyboard(self, selected: date) -> None:
        self._selected_date = selected
        self.dateSelected.emit(selected)
        self.update()

    def _date_at(self, point: QPointF) -> date | None:
        if point.y() < self.HEADER_HEIGHT or self.width() <= 0:
            return None
        column = min(6, max(0, int(point.x() / (self.width() / 7.0))))
        row_height = (self.height() - self.HEADER_HEIGHT) / 6.0
        if row_height <= 0:
            return None
        row = min(5, max(0, int((point.y() - self.HEADER_HEIGHT) / row_height)))
        return month_grid_start(self._year, self._month) + timedelta(days=row * 7 + column)

    def _task_at(self, point: QPointF) -> ScheduledTask | None:
        return next(
            (task for rect, task in reversed(self._task_hits) if rect.contains(point)), None
        )

    @staticmethod
    def _task_color(task: ScheduledTask) -> QColor:
        if task.status is TaskStatus.COMPLETED:
            return QColor("#7D899E")
        return QColor(
            {
                TaskPriority.NORMAL: "#4778D9",
                TaskPriority.ATTENTION: "#3B8D78",
                TaskPriority.IMPORTANT: "#CF7B2A",
                TaskPriority.URGENT: "#C74A55",
            }[task.priority]
        )


class CalendarPage(QFrame):
    monthChanged = Signal(int, int)
    taskSelected = Signal(object)
    taskActivated = Signal(object)
    createRequested = Signal(object)

    def __init__(self, *, timezone: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._timezone = timezone
        self._tasks: tuple[ScheduledTask, ...] = ()
        self._selected_task: ScheduledTask | None = None
        self.setObjectName("calendarCard")

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(18, 15, 18, 14)
        self._layout.setSpacing(10)
        toolbar = QHBoxLayout()
        self.previous_button = QPushButton("<")
        self.previous_button.setObjectName("calendarPrevious")
        self.previous_button.setToolTip("이전 달 (Page Up)")
        self.month_label = QLabel()
        self.month_label.setObjectName("calendarMonthTitle")
        self.next_button = QPushButton(">")
        self.next_button.setObjectName("calendarNext")
        self.next_button.setToolTip("다음 달 (Page Down)")
        self.today_button = QPushButton("오늘")
        self.today_button.setObjectName("calendarToday")
        toolbar.addWidget(self.previous_button)
        toolbar.addWidget(self.month_label)
        toolbar.addWidget(self.next_button)
        toolbar.addSpacing(6)
        toolbar.addWidget(self.today_button)
        toolbar.addStretch()
        self.create_button = QPushButton("+ 일정")
        self.create_button.setObjectName("calendarCreate")
        self.create_button.setProperty("calendarPrimary", True)
        toolbar.addWidget(self.create_button)
        self._layout.addLayout(toolbar)

        self.calendar = MonthCalendarWidget(timezone=timezone)
        self._layout.addWidget(self.calendar, 1)

        day_header = QHBoxLayout()
        self.day_label = QLabel()
        self.day_label.setObjectName("calendarDayTitle")
        self.day_count = QLabel()
        self.day_count.setObjectName("mutedText")
        day_header.addWidget(self.day_label)
        day_header.addWidget(self.day_count)
        day_header.addStretch()
        self.edit_button = QPushButton("선택 일정 수정")
        self.edit_button.setObjectName("calendarEdit")
        self.edit_button.setEnabled(False)
        day_header.addWidget(self.edit_button)
        self._layout.addLayout(day_header)

        self.day_list = QListWidget()
        self.day_list.setObjectName("calendarDayList")
        self.day_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.day_list.setAlternatingRowColors(True)
        self.day_list.setMaximumHeight(112)
        self.day_list.setMinimumHeight(70)
        self.day_list.setToolTip("일정을 더블 클릭하면 수정할 수 있습니다.")
        self._layout.addWidget(self.day_list)

        self.previous_button.clicked.connect(lambda: self._move_month(-1))
        self.next_button.clicked.connect(lambda: self._move_month(1))
        self.today_button.clicked.connect(self._go_today)
        self.create_button.clicked.connect(
            lambda: self.createRequested.emit(self.calendar.selected_date)
        )
        self.edit_button.clicked.connect(self._activate_selected)
        self.calendar.dateSelected.connect(self._select_date)
        self.calendar.moreRequested.connect(self._select_date)
        self.calendar.dateActivated.connect(self.createRequested)
        self.calendar.taskSelected.connect(self._select_task)
        self.calendar.taskActivated.connect(self.taskActivated)
        self.day_list.currentItemChanged.connect(self._on_day_item_changed)
        self.day_list.itemDoubleClicked.connect(self._on_day_item_activated)
        self._update_month_label()
        self._refresh_day_list()

    @property
    def visible_date_range(self) -> tuple[date, date]:
        return self.calendar.visible_date_range

    @property
    def selected_date(self) -> date:
        return self.calendar.selected_date

    def set_tasks(self, tasks: tuple[ScheduledTask, ...]) -> None:
        self._tasks = tasks
        self.calendar.set_tasks(tasks)
        self._refresh_day_list()

    def set_compact(self, compact: bool) -> None:
        self.calendar.set_compact(compact)
        self._layout.setContentsMargins(*(12, 8, 12, 9) if compact else (18, 15, 18, 14))
        self._layout.setSpacing(6 if compact else 10)
        self.day_list.setMinimumHeight(58 if compact else 70)
        self.day_list.setMaximumHeight(68 if compact else 112)
        self.create_button.setVisible(not compact)
        self.edit_button.setText("수정" if compact else "선택 일정 수정")

    def _move_month(self, delta: int) -> None:
        year, month = shift_month(*self.calendar.displayed_month, delta)
        target = date(year, month, min(self.selected_date.day, _days_in_month(year, month)))
        self._set_month_and_date(target)

    def _go_today(self) -> None:
        self._set_month_and_date(datetime.now(ZoneInfo(self._timezone)).date())

    def _select_date(self, selected: date) -> None:
        if (selected.year, selected.month) != self.calendar.displayed_month:
            self._set_month_and_date(selected)
            return
        self.calendar.set_selected_date(selected)
        self._selected_task = None
        self._refresh_day_list()

    def _set_month_and_date(self, selected: date) -> None:
        self.calendar.set_month(selected.year, selected.month)
        self.calendar.set_selected_date(selected)
        self._selected_task = None
        self._update_month_label()
        self._refresh_day_list()
        self.monthChanged.emit(selected.year, selected.month)

    def _update_month_label(self) -> None:
        year, month = self.calendar.displayed_month
        self.month_label.setText(f"{year}년 {MONTH_LABELS[month - 1]}")

    def _refresh_day_list(self) -> None:
        selected_id = self._selected_task.id if self._selected_task else None
        items = tasks_for_date(self._tasks, self.selected_date, self._timezone)
        self.day_list.clear()
        for task in items:
            item = QListWidgetItem(self._day_item_text(task))
            item.setData(Qt.ItemDataRole.UserRole, task)
            if task.status is TaskStatus.COMPLETED:
                item.setForeground(QColor("#7D899E"))
            self.day_list.addItem(item)
            if task.id == selected_id:
                self.day_list.setCurrentItem(item)
        self.day_label.setText(f"{self.selected_date.month}월 {self.selected_date.day}일")
        self.day_count.setText(f"{len(items)}개 일정")
        self.edit_button.setEnabled(self.day_list.currentItem() is not None)

    def _select_task(self, task: ScheduledTask) -> None:
        span = task_date_span(task, self._timezone)
        if span is not None and not (span[0] <= self.selected_date <= span[1]):
            self.calendar.set_selected_date(span[0])
        self._selected_task = task
        self._refresh_day_list()
        self.taskSelected.emit(task)

    def _on_day_item_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        task = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        self._selected_task = task if isinstance(task, ScheduledTask) else None
        self.edit_button.setEnabled(self._selected_task is not None)
        if self._selected_task is not None:
            self.taskSelected.emit(self._selected_task)

    def _on_day_item_activated(self, item: QListWidgetItem) -> None:
        task = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(task, ScheduledTask):
            self.taskActivated.emit(task)

    def _activate_selected(self) -> None:
        if self._selected_task is not None:
            self.taskActivated.emit(self._selected_task)

    def _day_item_text(self, task: ScheduledTask) -> str:
        zone = ZoneInfo(self._timezone)
        assert task.starts_at is not None
        local_start = task.starts_at.astimezone(zone)
        time_label = "종일" if task.all_day else local_start.strftime("%H:%M")
        pin = "★ " if task.is_pinned else ""
        done = "✓ " if task.status is TaskStatus.COMPLETED else ""
        repeat = "반복 · " if task.occurrence_start is not None else ""
        return f"{time_label}  ·  {repeat}{pin}{done}{task.title}"


def _days_in_month(year: int, month: int) -> int:
    next_year, next_month = shift_month(year, month, 1)
    return (date(next_year, next_month, 1) - timedelta(days=1)).day
