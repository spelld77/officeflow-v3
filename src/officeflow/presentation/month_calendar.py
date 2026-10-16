from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QFocusEvent,
    QHelpEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStyle,
    QStyleOptionButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.calendar_display import CalendarState, calendar_row_key, calendar_state
from officeflow.application.day_overview import DayOverviewResult
from officeflow.application.reminder_groups import (
    ReminderGroup,
    merge_day_reminders,
    reminder_scope_key,
)
from officeflow.application.reminder_preview import ReminderPreviewItem, ReminderPreviewResult
from officeflow.application.reminder_runtime import scope_label
from officeflow.application.tasks import CalendarOverview, ScheduledTask
from officeflow.presentation.calendar_style import priority_color
from officeflow.presentation.dates import WEEKDAYS, format_date
from officeflow.presentation.day_calendar import DayCalendarPanel
from officeflow.presentation.reminder_groups import (
    ReminderGroupDetailsDialog,
    group_summary,
    group_text,
    group_tooltip,
)
from officeflow.presentation.reminder_preview import ReminderPreviewDelegate
from officeflow.presentation.schedule_help import schedule_help
from officeflow.presentation.task_list import format_snoozed_time

WEEKDAY_LABELS = ("일", "월", "화", "수", "목", "금", "토")
MONTH_LABELS = tuple(f"{month}월" for month in range(1, 13))


def calendar_task_help(value: ScheduledTask | ReminderGroup, now: datetime, timezone: str) -> str:
    if isinstance(value, ReminderGroup):
        scheduled = value.scheduled
        exact = group_tooltip(value, timezone)
        if scheduled is None and value.task.recurrence_rule:
            return exact + "\n회차 일정 확인 필요"
        task = scheduled.display_task if scheduled else value.task
        return exact + "\n" + schedule_help(task, now)
    return value.title + "\n" + schedule_help(value.display_task, now)


class CalendarDayList(QListWidget):
    def __init__(self, timezone: str) -> None:
        super().__init__()
        self.timezone = timezone
        self.clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def viewportEvent(self, event: QEvent) -> bool:
        if isinstance(event, QHelpEvent) and event.type() is QEvent.Type.ToolTip:
            item = self.itemAt(event.pos())
            value = item.data(Qt.ItemDataRole.UserRole) if item else None
            if isinstance(value, (ScheduledTask, ReminderGroup)):
                QToolTip.showText(
                    event.globalPos(), calendar_task_help(value, self.clock(), self.timezone), self
                )
                return True
        return super().viewportEvent(event)


@dataclass(frozen=True, slots=True)
class CalendarSegment:
    task: ScheduledTask
    week: int
    start_column: int
    end_column: int
    lane: int
    continues_before: bool
    continues_after: bool


def calendar_segment_text(segment: CalendarSegment) -> str:
    """Keep secondary attachment information behind the task title."""
    prefix = "◀ " if segment.continues_before else ""
    if segment.task.occurrence_start is not None:
        prefix = f"반복 · {prefix}"
    attachment = " · 첨부" if segment.task.has_attachments else ""
    suffix = " ▶" if segment.continues_after else ""
    return prefix + segment.task.title + attachment + suffix


def month_grid_start(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first - timedelta(days=(first.weekday() + 1) % 7)


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
        occupied_masks: list[int] = []
        priority_lanes = [-1] * 7
        candidates.sort(
            key=lambda item: (
                calendar_state(item[0]),
                (item[1] - grid_start).days % 7,
                -(item[2] - item[1]).days,
                not item[0].is_pinned,
                item[0].starts_at or datetime.max.replace(tzinfo=ZoneInfo("UTC")),
                item[0].id or 0,
                item[0].occurrence_start or datetime.min.replace(tzinfo=ZoneInfo("UTC")),
            )
        )
        week_start = grid_start + timedelta(days=week * 7)
        for task, segment_start, segment_end, task_start, task_end in candidates:
            start_column = (segment_start - week_start).days
            end_column = (segment_end - week_start).days
            mask = ((1 << (end_column - start_column + 1)) - 1) << start_column
            priority = calendar_state(task) in {CalendarState.UNFINISHED, CalendarState.UNKNOWN}
            floor = 0 if priority else max(priority_lanes[start_column : end_column + 1]) + 1
            lane = next(
                (
                    index
                    for index, occupied in enumerate(occupied_masks)
                    if index >= floor and not occupied & mask
                ),
                max(floor, len(occupied_masks)),
            )
            while len(occupied_masks) <= lane:
                occupied_masks.append(0)
            occupied_masks[lane] |= mask
            if priority:
                for column in range(start_column, end_column + 1):
                    priority_lanes[column] = max(priority_lanes[column], lane)
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
    taskContextRequested = Signal(object, object)
    moreRequested = Signal(object)
    alarmRequested = Signal(object)

    HEADER_HEIGHT = 26.0
    DATE_HEIGHT = 23.0
    BAR_HEIGHT = 18.0

    def __init__(self, *, timezone: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        today = datetime.now(ZoneInfo(timezone)).date()
        self._timezone = timezone
        self._year = today.year
        self.clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        self._month = today.month
        self._selected_date = today
        self._tasks: tuple[ScheduledTask, ...] = ()
        self._day_counts: dict[date, int] = {}
        self._day_unfinished_counts: dict[date, int] | None = None
        self._previews_complete: bool | None = None
        self._more_labels: dict[date, str] = {}
        self._hidden_counts: dict[date, int] = {}
        self._alarm_counts: dict[date, int] = {}
        self._alarm_attention: dict[date, int] = {}
        self._alarms_complete = True
        self._summary_mode = False
        self._compact = False
        self._task_hits: list[tuple[QRectF, ScheduledTask]] = []
        self._more_hits: list[tuple[QRectF, date]] = []
        self._alarm_hits: list[tuple[QRectF, date]] = []
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
        start, end = self.visible_date_range
        self._day_counts = {
            start + timedelta(days=offset): len(
                tasks_for_date(tasks, start + timedelta(days=offset), self._timezone)
            )
            for offset in range((end - start).days)
        }
        self._summary_mode = False
        self._day_unfinished_counts = {
            start + timedelta(days=offset): sum(
                calendar_state(task) is CalendarState.UNFINISHED
                for task in tasks_for_date(tasks, start + timedelta(days=offset), self._timezone)
            )
            for offset in range((end - start).days)
        }
        self._previews_complete = True
        self.update()

    def set_overview(self, overview: CalendarOverview) -> None:
        self._tasks = overview.preview_tasks
        self._day_counts = dict(overview.day_counts)
        self._summary_mode = overview.summary_mode
        self._previews_complete = overview.previews_complete
        self._day_unfinished_counts = (
            dict(overview.day_unfinished_counts)
            if overview.day_unfinished_counts is not None
            else None
        )
        self.update()

    def set_compact(self, compact: bool) -> None:
        if compact != self._compact:
            self._compact = compact
            self.setMinimumHeight(220 if compact else 285)
            self.updateGeometry()
            self.update()

    def set_reminder_counts(
        self,
        counts: dict[date, int],
        *,
        complete: bool = True,
        attention: dict[date, int] | None = None,
    ) -> None:
        self._alarm_counts = counts
        self._alarms_complete = complete
        self._alarm_attention = attention or {}
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
        self._alarm_hits.clear()
        self._more_labels.clear()
        self._hidden_counts.clear()

        column_width = self.width() / 7.0
        row_height = max(1.0, (self.height() - self.HEADER_HEIGHT) / 6.0)
        inline_counts = row_height < self.DATE_HEIGHT + self.BAR_HEIGHT + 22
        for column, label in enumerate(WEEKDAY_LABELS):
            color = "#D14343" if column == 0 else "#2F6FED" if column == 6 else "#68738A"
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
            elif column == 0:
                painter.setPen(QColor("#D14343"))
            elif column == 6:
                painter.setPen(QColor("#2F6FED"))
            else:
                painter.setPen(QColor("#364158"))
            painter.drawText(day_rect, Qt.AlignmentFlag.AlignCenter, str(day.day))
            count = self._alarm_counts.get(day, 0)
            attention_count = self._alarm_attention.get(day, 0)
            if count or attention_count or not self._alarms_complete:
                painter.save()
                if inline_counts:
                    font = painter.font()
                    font.setPixelSize(11)
                    painter.setFont(font)
                text = f"알림 {count}{'' if self._alarms_complete else '+'}" if count else "알림 ?"
                if attention_count:
                    text = (
                        f"알림 {count} · 확인 필요 {attention_count}"
                        if count
                        else f"알림 확인 필요 {attention_count}"
                    )
                count_space = (
                    (44 if column_width >= 130 else 24)
                    if inline_counts and self._day_counts.get(day, 0)
                    else 0
                )
                rect = QRectF(
                    cell.left() + 34 + count_space,
                    cell.top() + 3,
                    max(1, cell.width() - 38 - count_space),
                    20,
                )
                if painter.fontMetrics().horizontalAdvance(text) > rect.width():
                    text = f"알{count}{'' if self._alarms_complete else '+'}" if count else "알?"
                    if attention_count:
                        text = (
                            f"알{count} !{attention_count}" if count else f"확인 !{attention_count}"
                        )
                painter.setPen(QColor("#7A4FAD"))
                if inline_counts and painter.fontMetrics().horizontalAdvance(text) > rect.width():
                    text = (
                        f"{count}{'+' if not self._alarms_complete else '!' if attention_count else ''}"
                        if count
                        else "?"
                    )
                painter.drawText(
                    rect,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    painter.fontMetrics().elidedText(
                        text, Qt.TextElideMode.ElideRight, int(rect.width())
                    ),
                )
                self._alarm_hits.append((rect, day))
                painter.restore()

        lane_limit = 2 if self._compact else 3
        available_lanes = max(1, int((row_height - self.DATE_HEIGHT - 22) // self.BAR_HEIGHT))
        count_only = self._summary_mode or row_height < self.DATE_HEIGHT + self.BAR_HEIGHT + 8
        lane_limit = min(lane_limit, available_lanes)
        segments = (
            () if count_only else build_calendar_segments(self._tasks, grid_start, self._timezone)
        )
        visible_task_days: set[tuple[int | None, datetime | None, date]] = set()
        visible_unfinished_days: set[tuple[int | None, datetime | None, date]] = set()
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
            title = calendar_segment_text(segment)
            text_rect = rect.adjusted(5, 0, -5, 0)
            elided = painter.fontMetrics().elidedText(
                title, Qt.TextElideMode.ElideRight, int(text_rect.width())
            )
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, elided)
            self._task_hits.append((rect, segment.task))
            week_start = grid_start + timedelta(days=segment.week * 7)
            for column in range(segment.start_column, segment.end_column + 1):
                visible_task_days.add(
                    (
                        segment.task.id,
                        segment.task.occurrence_start,
                        week_start + timedelta(days=column),
                    )
                )
                if calendar_state(segment.task) is CalendarState.UNFINISHED:
                    visible_unfinished_days.add(
                        (
                            segment.task.id,
                            segment.task.occurrence_start,
                            week_start + timedelta(days=column),
                        )
                    )

        for offset in range(42):
            day = grid_start + timedelta(days=offset)
            visible = sum(
                1
                for task in tasks_for_date(self._tasks, day, self._timezone)
                if (task.id, task.occurrence_start, day) in visible_task_days
            )
            hidden = max(0, self._day_counts.get(day, 0) - visible)
            if hidden == 0:
                continue
            self._hidden_counts[day] = hidden
            row, column = divmod(offset, 7)
            hidden_unfinished = (
                max(
                    0,
                    self._day_unfinished_counts.get(day, 0)
                    - sum(key[2] == day for key in visible_unfinished_days),
                )
                if self._day_unfinished_counts is not None
                else None
            )
            accent = bool(hidden_unfinished)
            outline = "#AFC5F5" if accent else "#D9E0EA"
            background = "#E3ECFF" if accent else "#F3F6FA"
            ink = "#245CCC" if accent else "#526078"
            if inline_counts:
                # At low height use separate inline hit regions, not two
                # overlapping bottom badges for task and reminder counts.
                label = (
                    f"남음 {hidden_unfinished}"
                    if accent and column_width >= 130
                    else str(hidden_unfinished if accent else hidden)
                )
                self._more_labels[day] = label
                badge_width = 42 if column_width >= 130 else 22
                rect = QRectF(
                    column * column_width + 33,
                    self.HEADER_HEIGHT + row * row_height + 3,
                    badge_width,
                    18,
                )
                painter.setPen(QPen(QColor(outline), 1))
                painter.setBrush(QColor(background))
                painter.drawRoundedRect(rect, 4, 4)
                painter.setPen(QColor(ink))
                painter.drawText(
                    rect,
                    Qt.AlignmentFlag.AlignCenter,
                    painter.fontMetrics().elidedText(
                        label, Qt.TextElideMode.ElideRight, badge_width - 2
                    ),
                )
                self._more_hits.append((rect, day))
                continue
            if count_only:
                label = f"{hidden}개 일정" if column_width >= 130 else f"{hidden}개"
            else:
                label = f"+{hidden}개 더 보기" if column_width >= 130 else f"+{hidden}개"
            if accent:
                label = (
                    f"미완료 {hidden_unfinished}개" + (" 더" if not count_only else "")
                    if column_width >= 130
                    else f"남음 {hidden_unfinished}"
                )
            elif hidden_unfinished is None and not self._previews_complete and column_width >= 130:
                label = f"+{hidden} · 목록 확인"
            self._more_labels[day] = label
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
            painter.setPen(QPen(QColor(outline), 1))
            painter.setBrush(QColor(background))
            painter.drawRoundedRect(rect, 6, 6)
            painter.setPen(QColor(ink))
            painter.drawText(
                rect,
                Qt.AlignmentFlag.AlignCenter,
                label,
            )
            self._more_hits.append((rect, day))

        self.setAccessibleDescription(
            "; ".join(self._count_tooltip(day) for day in self._day_counts if self._day_counts[day])
        )

        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#2F6FED"), 1, Qt.PenStyle.DashLine))
            painter.drawRect(QRectF(self.rect()).adjusted(1, 1, -2, -2))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() is Qt.MouseButton.RightButton:
            task = self._task_at(event.position())
            if task is not None:
                self.setFocus()
                self.taskSelected.emit(task)
                self.taskContextRequested.emit(task, event.globalPosition().toPoint())
                event.accept()
                return
        if event.button() is not Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.setFocus()
        for rect, hit_day in self._alarm_hits:
            if rect.contains(event.position()):
                self._selected_date = hit_day
                self.alarmRequested.emit(hit_day)
                self.update()
                return
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
        if any(rect.contains(event.position()) for rect, _day in self._alarm_hits):
            return
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
            point = QPointF(self.mapFromGlobal(self.cursor().pos()))
            day = self._date_at(point)
            if day is not None and any(
                rect.contains(point) for rect, _day in (*self._alarm_hits, *self._more_hits)
            ):
                self.setToolTip(
                    self._count_tooltip(day)
                    + (" · 일부 조회" if not self._alarms_complete else "")
                    + "\n누르면 아래 목록에서 확인합니다."
                )
                return super().event(event)
            task = self._task_at(point)
            self.setToolTip(
                calendar_task_help(task, self.clock(), self._timezone) + "\n첨부파일 있음"
                if task is not None and task.has_attachments
                else calendar_task_help(task, self.clock(), self._timezone)
                if task is not None
                else "날짜를 더블 클릭하면 일정을 등록합니다."
            )
        return super().event(event)

    def _count_tooltip(self, day: date) -> str:
        state = (
            f" · 미완료 {self._day_unfinished_counts.get(day, 0)}개"
            if self._day_unfinished_counts is not None
            else " · 업무 상태 일부만 조회"
        )
        hidden = f" · 숨은 일정 {self._hidden_counts[day]}개" if day in self._hidden_counts else ""
        return (
            f"{day.month}월 {day.day}일 · 일정 {self._day_counts.get(day, 0)}개{state} · "
            f"알림 {self._alarm_counts.get(day, 0)}건 · 발생 기록 확인 필요 {self._alarm_attention.get(day, 0)}건{hidden}"
        )

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
        if calendar_state(task) in {CalendarState.COMPLETED, CalendarState.INACTIVE}:
            return QColor("#7D899E")
        return QColor(priority_color(task.priority))


class CalendarPage(QFrame):
    modeChanged = Signal(str)
    dayScheduleMoreRequested = Signal()
    daySchedulePageRequested = Signal(object)
    alarmSelected = Signal(object)
    alarmActivated = Signal(object)
    alarmContextRequested = Signal(object, object)
    monthChanged = Signal(int, int)
    dateSelected = Signal(object)
    taskSelected = Signal(object)
    taskActivated = Signal(object)
    attachmentsRequested = Signal(object)
    taskSelectionCleared = Signal()
    taskContextRequested = Signal(object, object)
    createRequested = Signal(object)
    alarmMoreRequested = Signal()
    reminderSettingsRequested = Signal(object)

    def __init__(self, *, timezone: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._timezone = timezone
        self._tasks: tuple[ScheduledTask, ...] = ()
        self._day_tasks: tuple[ScheduledTask, ...] = ()
        self._reminder_preview = ReminderPreviewResult((), True)
        self._day_alarm_override: tuple[date, ReminderPreviewResult] | None = None
        self._alarm_focus_date: date | None = None
        self._reminder_details: ReminderGroupDetailsDialog | None = None
        self._selected_task: ScheduledTask | None = None
        self._snoozed_until: dict[tuple[int, datetime | None], datetime] = {}
        self._compact = False
        self.mode = "month"
        self._return_mode = "month"
        self._records_available = True
        self._attachments_available = True
        self._normal_splitter_sizes = [380, 170]
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
        self.summary_label = QLabel("일정이 많아 개수로 표시")
        self.summary_label.setObjectName("mutedText")
        self.summary_label.hide()
        toolbar.addWidget(self.summary_label)
        toolbar.addStretch()
        self.view_button = QPushButton("하루 보기", self)
        self.view_button.setProperty("calendarAction", True)
        self.view_button.clicked.connect(self._toggle_view)
        toolbar.addWidget(self.view_button)
        self.day_alarm_button = QPushButton("하루 알림", self)
        self.day_alarm_button.setProperty("calendarAction", True)
        self.day_alarm_button.setToolTip("선택 날짜에 등록된 모든 업무의 알림")
        self.day_alarm_button.clicked.connect(lambda: self.set_mode("alarms"))
        toolbar.addWidget(self.day_alarm_button)
        self.create_button = QPushButton("+ 일정")
        self.create_button.setObjectName("calendarCreate")
        self.create_button.setProperty("calendarPrimary", True)
        toolbar.addWidget(self.create_button)
        self._layout.addLayout(toolbar)

        self.calendar_splitter = QSplitter(Qt.Orientation.Vertical)
        self.calendar_splitter.setObjectName("calendarDaySplitter")
        self.calendar_splitter.setChildrenCollapsible(False)
        self.calendar_splitter.setHandleWidth(8)
        self.calendar_splitter.setOpaqueResize(True)

        self.calendar = MonthCalendarWidget(timezone=timezone)
        self.calendar.setMinimumHeight(220)
        self.calendar_splitter.addWidget(self.calendar)

        self.day_panel = QWidget()
        self.day_panel.setObjectName("calendarDayPanel")
        self.day_panel.setMinimumHeight(180)
        self.day_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        day_panel_layout = QVBoxLayout(self.day_panel)
        day_panel_layout.setContentsMargins(0, 0, 0, 0)
        day_panel_layout.setSpacing(6)

        day_header = QHBoxLayout()
        day_header.setSpacing(5)
        self.day_label = QLabel()
        self.day_label.setObjectName("calendarDayTitle")
        self.day_count = QLabel()
        self.day_count.setMinimumWidth(0)
        self.day_count.setObjectName("calendarDayCount")
        day_header.addWidget(self.day_label)
        self.day_more_hint = QLabel("↓ 목록을 스크롤해 전체 일정을 확인하세요")
        self.day_more_hint.setObjectName("calendarDayMoreHint")
        self.day_more_hint.hide()
        day_header.addStretch()
        self.expand_list_button = QPushButton("목록 확대")
        self.expand_list_button.setObjectName("calendarExpandDayList")
        self.expand_list_button.setCheckable(True)
        self.expand_list_button.setToolTip("달력을 줄이고 선택 날짜의 일정 목록을 크게 봅니다.")
        self.expand_list_button.setProperty("calendarAction", True)
        day_header.addWidget(self.expand_list_button)
        self.records_button = QPushButton("업무 보기")
        self.records_button.setObjectName("calendarRecords")
        self.records_button.setToolTip(
            "선택한 업무의 체크리스트 · 완료 요약 · 업무일지 확인 (더블클릭)"
        )
        self.attachments_button = QPushButton("첨부")
        self.attachments_button.setObjectName("calendarAttachments")
        self.attachments_button.setToolTip("선택한 업무의 첨부파일 목록 보기 · 관리")
        self.more_button = QPushButton("⋯")
        self.more_button.setObjectName("calendarTaskActions")
        self.more_button.setAccessibleName("선택 일정 메뉴")
        self.more_button.setToolTip("체크리스트 · 업무일지 · 완료 · 일정 수정 등")
        for button in (self.records_button, self.attachments_button, self.more_button):
            button.setProperty("calendarAction", True)
            button.setEnabled(False)
            day_header.addWidget(button)
        day_panel_layout.addLayout(day_header)
        alarm_summary = QHBoxLayout()
        alarm_summary.addWidget(self.day_count, 1)
        self.day_count.setWordWrap(True)
        self.reminder_details_button = QPushButton("선택 업무 알림")
        self.reminder_details_button.setProperty("calendarAction", True)
        self.reminder_details_button.setEnabled(False)
        self.reminder_details_button.clicked.connect(self._show_reminder_details)
        alarm_summary.addWidget(self.reminder_details_button)
        self.alarm_more_button = QPushButton("알림 더 조회…")
        self.alarm_more_button.setProperty("calendarAction", True)
        self.alarm_more_button.hide()
        self.alarm_more_button.clicked.connect(self.alarmMoreRequested.emit)
        alarm_summary.addWidget(self.alarm_more_button)
        day_panel_layout.addLayout(alarm_summary)

        self.day_list = CalendarDayList(self._timezone)
        self.day_list.setObjectName("calendarDayList")
        self.day_list.setItemDelegate(ReminderPreviewDelegate(self._timezone, self.day_list))
        self.day_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.day_list.setAlternatingRowColors(True)
        self.day_list.setUniformItemSizes(True)
        self.day_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerItem)
        self.day_list.setMinimumHeight(112)
        self.day_list.setToolTip(
            "일정을 더블클릭하면 업무 기록이 열립니다. 일정 수정은 ⋯ 또는 오른쪽 클릭 메뉴에서 합니다."
        )
        self.day_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        day_panel_layout.addWidget(self.day_list, 1)
        day_panel_layout.addWidget(self.day_more_hint)
        self.calendar_splitter.addWidget(self.day_panel)
        self.calendar_splitter.setStretchFactor(0, 3)
        self.calendar_splitter.setStretchFactor(1, 2)
        self.calendar_splitter.setSizes(self._normal_splitter_sizes)
        self._layout.addWidget(self.calendar_splitter, 1)
        self.day_view = DayCalendarPanel(timezone, self)
        self.day_view.hide()
        self._layout.addWidget(self.day_view, 1)
        self.day_view.selected.connect(self._day_selected)
        self.day_view.activated.connect(self._day_activated)
        self.day_view.contextRequested.connect(self._day_context)
        self.day_view.scheduleMoreRequested.connect(self.dayScheduleMoreRequested.emit)
        self.day_view.scheduleCursorRequested.connect(self.daySchedulePageRequested.emit)
        self.day_view.selectionCleared.connect(self.taskSelectionCleared.emit)
        self.day_view.alarmMoreRequested.connect(self.alarmMoreRequested.emit)

        self.previous_button.clicked.connect(lambda: self._move_month(-1))
        self.next_button.clicked.connect(lambda: self._move_month(1))
        self.today_button.clicked.connect(self._go_today)
        self.create_button.clicked.connect(
            lambda: self.createRequested.emit(self.calendar.selected_date)
        )
        self.records_button.clicked.connect(self._activate_selected)
        self.attachments_button.clicked.connect(self._open_selected_attachments)
        self.more_button.clicked.connect(self._show_selected_context_menu)
        self.expand_list_button.toggled.connect(self._toggle_day_list_expanded)
        self.calendar_splitter.splitterMoved.connect(self._remember_splitter_sizes)
        self.calendar.dateSelected.connect(self._select_date)
        self.calendar.moreRequested.connect(self._select_date)
        self.calendar.alarmRequested.connect(self._select_alarm_date)
        self.calendar.dateActivated.connect(self.createRequested)
        self.calendar.taskSelected.connect(self._select_task)
        self.calendar.taskActivated.connect(self.taskActivated)
        self.calendar.taskContextRequested.connect(self.taskContextRequested)
        self.day_list.currentItemChanged.connect(self._on_day_item_changed)
        self.day_list.itemDoubleClicked.connect(self._on_day_item_activated)
        self.day_list.customContextMenuRequested.connect(self._show_day_item_context_menu)
        self._update_month_label()
        self._refresh_day_list()

    @property
    def visible_date_range(self) -> tuple[date, date]:
        return self.calendar.visible_date_range

    def _toggle_view(self) -> None:
        self.set_mode(
            self._return_mode
            if self.mode == "alarms"
            else "day"
            if self.mode == "month"
            else "month"
        )

    def set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        if mode == "alarms":
            self._return_mode = self.mode
        self.mode = mode
        self.calendar_splitter.setVisible(mode == "month")
        self.summary_label.hide()
        self.day_view.set_day(self.selected_date)
        self.day_view.setVisible(mode != "month")
        self.day_view.set_mode(mode)
        self.view_button.setText(
            "돌아가기" if mode == "alarms" else "하루 보기" if mode == "month" else "월 보기"
        )
        self.previous_button.setToolTip("이전 달" if mode == "month" else "이전 날짜")
        self.next_button.setToolTip("다음 달" if mode == "month" else "다음 날짜")
        self._update_month_label()
        self.modeChanged.emit(mode)

    def set_day_overview(self, result: DayOverviewResult) -> None:
        self.day_view.set_result(result)

    def _day_selected(self, value: object) -> None:
        if isinstance(value, ScheduledTask):
            self._selected_task = value
            self.taskSelected.emit(value)
        elif isinstance(value, ReminderPreviewItem):
            self.alarmSelected.emit(value)

    def _day_activated(self, value: object) -> None:
        if isinstance(value, ScheduledTask):
            self.taskActivated.emit(value)
        elif isinstance(value, ReminderPreviewItem):
            self.alarmActivated.emit(value)

    def _day_context(self, value: object, position: QPoint) -> None:
        if isinstance(value, ScheduledTask):
            self.taskContextRequested.emit(value, position)
        elif isinstance(value, ReminderPreviewItem):
            self.alarmContextRequested.emit(value, position)

    def _update_day_alarm_count(self, result: ReminderPreviewResult) -> None:
        alarms = result.for_date(self.selected_date, self._timezone)
        count = sum(item.is_pending or item.needs_attention for item in alarms)
        self.day_alarm_button.setText(f"하루 알림 {count}{'' if result.complete else '+'}건")
        self._fit_toolbar_controls()

    @property
    def selected_date(self) -> date:
        return self.calendar.selected_date

    def set_tasks(self, tasks: tuple[ScheduledTask, ...]) -> None:
        self._tasks = tasks
        self._day_tasks = tasks_for_date(tasks, self.selected_date, self._timezone)
        self.calendar.set_tasks(tasks)
        self._refresh_day_list()

    def set_overview(self, overview: CalendarOverview) -> None:
        self._tasks = overview.preview_tasks
        self._day_tasks = ()
        self.calendar.set_overview(overview)
        self.summary_label.setVisible(overview.summary_mode)
        # A month refresh loads the day rows immediately afterwards. Keep the
        # identity across that temporary empty state, but disable row actions.
        self._refresh_day_list(preserve_selection=True)

    def set_day_tasks(self, tasks: tuple[ScheduledTask, ...]) -> None:
        self._day_tasks = tasks
        self._refresh_day_list()

    def set_reminder_preview(self, result: ReminderPreviewResult) -> None:
        self._reminder_preview = result
        selected = (
            self._day_alarm_override[1]
            if self._day_alarm_override and self._day_alarm_override[0] == self.selected_date
            else result
        )
        self.day_view.set_reminders(selected)
        self._update_day_alarm_count(selected)
        if self.mode != "month":
            return
        start, end = self.visible_date_range
        counts = {
            start + timedelta(days=i): sum(
                item.is_pending
                for item in result.for_date(start + timedelta(days=i), self._timezone)
            )
            for i in range((end - start).days)
        }
        attention = {
            day: sum(item.needs_attention for item in result.for_date(day, self._timezone))
            for day in counts
        }
        self.calendar.set_reminder_counts(counts, complete=result.complete, attention=attention)
        self._refresh_day_list(preserve_selection=True)

    def set_day_reminder_preview(self, day: date, result: ReminderPreviewResult) -> None:
        self._day_alarm_override = (day, result)
        if day == self.selected_date:
            self.day_view.set_reminders(result)
            self._update_day_alarm_count(result)
            self._refresh_day_list(preserve_selection=True)

    def set_snoozed_reminders(
        self, reminders: Mapping[tuple[int, datetime | None], datetime]
    ) -> None:
        self._snoozed_until = dict(reminders)
        self._refresh_day_list()

    def set_compact(self, compact: bool) -> None:
        self._compact = compact
        self.calendar.set_compact(compact)
        self._layout.setContentsMargins(*(12, 8, 12, 9) if compact else (18, 15, 18, 14))
        self._layout.setSpacing(6 if compact else 10)
        self.calendar.setMinimumHeight(146 if compact else 220)
        self._fit_toolbar_controls()
        self._update_day_list_minimum_height()
        self.create_button.setVisible(not compact)
        self._update_more_hint(self.day_list.count())

    def _fit_toolbar_controls(self) -> None:
        self.month_label.ensurePolished()
        self.month_label.setMinimumWidth(
            max(120, self.month_label.fontMetrics().horizontalAdvance(self.month_label.text()) + 4)
        )
        self.month_label.setMinimumHeight(self.month_label.fontMetrics().height() + 2)
        navigation = (self.previous_button, self.next_button, self.today_button)
        heights = []
        for button in (*navigation, self.view_button, self.day_alarm_button, self.create_button):
            if button in navigation:
                button.setMinimumHeight(0)
                button.setMaximumHeight(16_777_215)
            button.ensurePolished()
            option = QStyleOptionButton()
            button.initStyleOption(option)
            content = button.style().subElementRect(
                QStyle.SubElement.SE_PushButtonContents, option, button
            )
            metrics = button.fontMetrics()
            width = metrics.horizontalAdvance(button.text()) + button.width() - content.width() + 2
            button.setMinimumWidth(max(46 if button in navigation[:2] else 0, width))
            height = metrics.height() + button.height() - content.height() + 2
            if button in navigation:
                heights.append(height)
            else:
                button.setMinimumHeight(height)
        height = max(30 if self._compact else 38, *heights)
        for button in navigation:
            button.setFixedHeight(height)

    def set_record_capabilities(self, *, records: bool, attachments: bool) -> None:
        self._records_available = records
        self._attachments_available = records and attachments
        self._update_action_buttons()

    def _day_row_height(self) -> int:
        # Native Windows list decoration otherwise makes a one-line item much
        # taller than its text. Reserve readable text + padding explicitly.
        return max(32, self.day_list.fontMetrics().height() + 12)

    def _update_day_list_minimum_height(self) -> None:
        self.day_list.setMinimumHeight(
            max(104 if self._compact else 112, self._day_row_height() * 3 + 8)
        )
        panel_layout = self.day_panel.layout()
        assert panel_layout is not None
        self.day_panel.setMinimumHeight(
            max(170 if self._compact else 180, panel_layout.minimumSize().height())
        )

    def _update_action_buttons(self) -> None:
        row = self.day_list.currentItem()
        self.reminder_details_button.setEnabled(
            row is not None and isinstance(row.data(Qt.ItemDataRole.UserRole), ReminderGroup)
        )
        selected = self._selected_task if self.day_list.currentItem() is not None else None
        self.records_button.setEnabled(selected is not None and self._records_available)
        self.attachments_button.setEnabled(selected is not None and self._attachments_available)
        self.more_button.setEnabled(selected is not None)
        if selected is not None:
            self.attachments_button.setToolTip(
                "등록한 첨부파일 목록 보기 · 관리"
                if selected.has_attachments
                else "첨부파일이 없습니다. 목록을 열어 파일을 추가할 수 있습니다."
            )

    def _toggle_day_list_expanded(self, expanded: bool) -> None:
        self.expand_list_button.setText("달력 확대" if expanded else "목록 확대")
        sizes = self.calendar_splitter.sizes()
        if expanded:
            if len(sizes) == 2 and all(size > 0 for size in sizes):
                self._normal_splitter_sizes = sizes
            total = max(sum(sizes), 500)
            calendar_size = max(self.calendar.minimumHeight(), int(total * 0.35))
            self.calendar_splitter.setSizes(
                [
                    calendar_size,
                    max(self.day_panel.minimumHeight(), total - calendar_size),
                ]
            )
        else:
            self.calendar_splitter.setSizes(self._normal_splitter_sizes)

    def _remember_splitter_sizes(self, _position: int, _index: int) -> None:
        if self.expand_list_button.isChecked():
            return
        sizes = self.calendar_splitter.sizes()
        if len(sizes) == 2 and all(size > 0 for size in sizes):
            self._normal_splitter_sizes = sizes

    def _move_month(self, delta: int) -> None:
        if self.mode != "month":
            self._select_date(self.selected_date + timedelta(days=delta))
            return
        year, month = shift_month(*self.calendar.displayed_month, delta)
        target = date(year, month, min(self.selected_date.day, _days_in_month(year, month)))
        self._set_month_and_date(target)

    def _go_today(self) -> None:
        self._set_month_and_date(self.calendar.clock().astimezone(ZoneInfo(self._timezone)).date())

    def _select_alarm_date(self, selected: date) -> None:
        self._select_date(selected, focus_alarm=True)
        # dateSelected/monthChanged can synchronously replace the temporary rows.
        # Focus after those handlers too, rather than keeping an empty-list scroll offset.
        self._alarm_focus_date = selected
        self._focus_pending_alarm()

    def _focus_pending_alarm(self) -> None:
        if self._alarm_focus_date is None:
            return
        if self._alarm_focus_date != self.selected_date:
            self._alarm_focus_date = None
            return
        result = (
            self._day_alarm_override[1]
            if self._day_alarm_override and self._day_alarm_override[0] == self.selected_date
            else self._reminder_preview
        )
        alarms = result.for_date(self.selected_date, self._timezone)
        if not alarms:
            return  # A later preview refresh can fulfil the same date's focus request.
        selected_alarm = next((alarm for alarm in alarms if alarm.is_pending), alarms[0])
        for row in range(self.day_list.count()):
            item = self.day_list.item(row)
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, ReminderGroup) and data.key == reminder_scope_key(selected_alarm):
                self.day_list.setCurrentItem(item)
                self.day_list.doItemsLayout()
                self.day_list.scrollToItem(item)
                self._alarm_focus_date = None
                return

    def _select_date(self, selected: date, *, focus_alarm: bool = False) -> None:
        self._alarm_focus_date = selected if focus_alarm else None
        if (selected.year, selected.month) != self.calendar.displayed_month:
            self._set_month_and_date(selected)
            return
        self.calendar.set_selected_date(selected)
        self.day_view.set_day(selected)
        self.taskSelectionCleared.emit()
        self._selected_task = None
        self._day_tasks = ()
        self._refresh_day_list()
        self.dateSelected.emit(selected)

    def _set_month_and_date(self, selected: date) -> None:
        self.calendar.set_month(selected.year, selected.month)
        self.calendar.set_selected_date(selected)
        self.day_view.set_day(selected)
        self.taskSelectionCleared.emit()
        self._selected_task = None
        self._day_tasks = ()
        self._update_month_label()
        self._refresh_day_list()
        self.monthChanged.emit(selected.year, selected.month)

    def _update_month_label(self) -> None:
        year, month = self.calendar.displayed_month
        self.month_label.setText(
            f"{year}년 {MONTH_LABELS[month - 1]}"
            if self.mode == "month"
            else format_date(self.selected_date)
        )
        self._fit_toolbar_controls()

    def _refresh_day_list(self, *, preserve_selection: bool = False) -> None:
        if self.mode != "month":
            self._update_month_label()
            return
        selected_key = (
            (
                self._selected_task.id,
                self._selected_task.occurrence_start
                if self._selected_task.task.recurrence_rule
                else None,
            )
            if self._selected_task is not None
            else None
        )
        result = (
            self._day_alarm_override[1]
            if self._day_alarm_override and self._day_alarm_override[0] == self.selected_date
            else self._reminder_preview
        )
        alarms = result.for_date(self.selected_date, self._timezone)
        items = tuple(sorted(merge_day_reminders(self._day_tasks, alarms), key=calendar_row_key))
        scroll = self.day_list.verticalScrollBar().value()
        anchor = self.day_list.itemAt(QPoint(2, 2)) if preserve_selection else None
        anchor_data = anchor.data(Qt.ItemDataRole.UserRole) if anchor else None
        anchor_key = (
            (anchor_data.id, anchor_data.occurrence_start) if anchor_data is not None else None
        )
        anchor_offset = self.day_list.visualItemRect(anchor).top() if anchor else 0
        with QSignalBlocker(self.day_list):
            self.day_list.clear()
            for task in items:
                text = (
                    self._group_row_text(task)
                    if isinstance(task, ReminderGroup)
                    else self._day_item_text(task)
                )
                item = QListWidgetItem(text)
                item.setToolTip(
                    group_tooltip(task, self._timezone) if isinstance(task, ReminderGroup) else text
                )
                item.setSizeHint(QSize(0, self._day_row_height()))
                item.setData(Qt.ItemDataRole.UserRole, task)
                closed = calendar_state(task) in {CalendarState.COMPLETED, CalendarState.INACTIVE}
                item.setForeground(QColor("#7D899E" if closed else "#172033"))
                self.day_list.addItem(item)
                key = (
                    task.key
                    if isinstance(task, ReminderGroup)
                    else (task.id, task.occurrence_start if task.task.recurrence_rule else None)
                )
                matched = key == selected_key and selected_key is not None
                if matched:
                    self.day_list.setCurrentItem(item)
        current = self.day_list.currentItem()
        if current is not None or not preserve_selection:
            self._on_day_item_changed(current, None)
        self.day_list.verticalScrollBar().setValue(scroll)
        if anchor_key is not None:
            for row in range(self.day_list.count()):
                candidate = self.day_list.item(row)
                data = candidate.data(Qt.ItemDataRole.UserRole)
                if (data.id, data.occurrence_start) == anchor_key:
                    self.day_list.doItemsLayout()
                    self.day_list.scrollToItem(
                        candidate, QAbstractItemView.ScrollHint.PositionAtTop
                    )
                    bar = self.day_list.verticalScrollBar()
                    bar.setValue(bar.value() - anchor_offset)
                    break
        self.day_label.setText(
            f"{self.selected_date.month}월 {self.selected_date.day}일({WEEKDAYS[self.selected_date.weekday()]})"
        )
        has_many = len(items) >= 3
        has_overflow = len(items) > 3
        self.day_count.setText(f"총 {len(items)}개 일정" if items else "일정 없음")
        unfinished_count = sum(calendar_state(item) is CalendarState.UNFINISHED for item in items)
        if items:
            self.day_count.setText(self.day_count.text() + f" · 미완료 {unfinished_count}")
        if alarms or not result.complete:
            active = sum(item.is_pending for item in alarms)
            attention = sum(item.needs_attention for item in alarms)
            history = len(alarms) - active - attention
            self.day_count.setText(
                f"업무 {len(items)}개 · 알림 {active}건 · 처리 이력 {history}건"
                + f" · 미완료 {unfinished_count}"
                + (f" · 발생 기록 확인 필요 {attention}건" if attention else "")
                + f"{'' if result.complete else ' 이상 (일부 조회)'}"
            )
        self.alarm_more_button.setVisible(not result.complete)
        self.day_count.setProperty("hasMany", has_many)
        self.day_count.style().unpolish(self.day_count)
        self.day_count.style().polish(self.day_count)
        self.day_more_hint.setVisible(has_overflow)
        self._update_more_hint(len(items))
        self.day_count.setToolTip(self.day_count.text())
        if self._reminder_details:
            details_key = self._reminder_details.group_key
            self._reminder_details.set_group(
                next(
                    (g for g in items if isinstance(g, ReminderGroup) and g.key == details_key),
                    None,
                ),
                complete=result.complete,
            )
        self.day_list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn
            if has_overflow
            else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.day_list.setAccessibleDescription(
            f"선택한 날짜의 일정 {len(items)}개. "
            + ("목록을 스크롤하거나 확대해 모두 확인하세요." if has_overflow else "")
        )
        self._update_action_buttons()
        self._update_day_list_minimum_height()
        self._focus_pending_alarm()

    def _update_more_hint(self, item_count: int) -> None:
        remaining_count = max(0, item_count - 3)
        if self._compact:
            self.day_more_hint.setText(f"↓ 아래에 {remaining_count}개 더 있음")
        else:
            self.day_more_hint.setText(
                f"↓ 아래에 {remaining_count}개 더 있음 · 스크롤하거나 목록 확대"
            )

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
        self._selected_task = self._action_task(task)
        self.reminder_details_button.setEnabled(isinstance(task, ReminderGroup))
        self._update_action_buttons()
        if self._selected_task is not None:
            self.taskSelected.emit(self._selected_task)
        else:
            self.taskSelectionCleared.emit()

    def _on_day_item_activated(self, item: QListWidgetItem) -> None:
        task = self._action_task(item.data(Qt.ItemDataRole.UserRole))
        if isinstance(task, ScheduledTask):
            self.taskActivated.emit(task)

    def _show_day_item_context_menu(self, position: QPoint) -> None:
        item = self.day_list.itemAt(position)
        if item is None:
            return
        self.day_list.setCurrentItem(item)
        task = self._action_task(item.data(Qt.ItemDataRole.UserRole))
        if isinstance(task, ScheduledTask):
            self.taskContextRequested.emit(
                task,
                self.day_list.viewport().mapToGlobal(position),
            )

    @staticmethod
    def _action_task(value: object) -> ScheduledTask | None:
        if isinstance(value, ReminderGroup):
            if value.scheduled:
                return value.scheduled
            value = value.representative
        if isinstance(value, ScheduledTask):
            return value
        if isinstance(value, ReminderPreviewItem):
            starts_at = value.occurrence_start or value.task.starts_at or value.effective_at
            ends_at = value.task.ends_at
            if (
                value.occurrence_start
                and value.task.recurrence_rule
                and value.task.starts_at
                and ends_at
            ):
                ends_at = starts_at + (ends_at - value.task.starts_at)
            return ScheduledTask(
                value.task,
                starts_at,
                ends_at,
                occurrence_start=value.occurrence_start if value.task.recurrence_rule else None,
                occurrence_status=value.occurrence_status,
                completed_at=value.task.completed_at,
            )
        return None

    def _group_row_text(self, group: ReminderGroup) -> str:
        if group.scheduled:
            return (
                self._day_item_text(group.scheduled) + " · " + group_summary(group, self._timezone)
            )
        marker = "✓ " if calendar_state(group) is CalendarState.COMPLETED else ""
        if calendar_state(group) is CalendarState.INACTIVE:
            marker = f"[{scope_label(group.task, group.representative.occurrence_status)}] "
        return "[알림만] " + marker + group_text(group, self._timezone)

    def _show_reminder_details(self) -> None:
        row = self.day_list.currentItem()
        group = row.data(Qt.ItemDataRole.UserRole) if row else None
        if not isinstance(group, ReminderGroup):
            return
        result = (
            self._day_alarm_override[1]
            if self._day_alarm_override and self._day_alarm_override[0] == self.selected_date
            else self._reminder_preview
        )
        if self._reminder_details:
            self._reminder_details.set_group(group, complete=result.complete)
            self._reminder_details.show()
            self._reminder_details.raise_()
            return
        dialog = ReminderGroupDetailsDialog(
            group, self._timezone, complete=result.complete, parent=self
        )
        dialog.recordRequested.connect(self._open_alarm_records)
        dialog.settingsRequested.connect(self.reminderSettingsRequested.emit)
        self._reminder_details = dialog

        def finished(_result: int) -> None:
            self._reminder_details = None
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.show()

    def _open_alarm_records(self, alarm: ReminderPreviewItem) -> None:
        task = self._action_task(alarm)
        if task:
            self.taskActivated.emit(task)

    def shutdown(self) -> None:
        self.day_view.shutdown()
        if self._reminder_details:
            self._reminder_details.reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)

    def _activate_selected(self) -> None:
        if self._selected_task is not None:
            self.taskActivated.emit(self._selected_task)

    def _open_selected_attachments(self) -> None:
        if self._selected_task is not None:
            self.attachmentsRequested.emit(self._selected_task)

    def _show_selected_context_menu(self) -> None:
        if self._selected_task is not None:
            self.taskContextRequested.emit(
                self._selected_task,
                self.more_button.mapToGlobal(QPoint(0, self.more_button.height())),
            )

    def _day_item_text(self, task: ScheduledTask) -> str:
        zone = ZoneInfo(self._timezone)
        assert task.starts_at is not None
        local_start = task.starts_at.astimezone(zone)
        time_label = "종일" if task.all_day else local_start.strftime("%H:%M")
        pin = "★ " if task.is_pinned else ""
        done = "✓ " if calendar_state(task) is CalendarState.COMPLETED else ""
        if calendar_state(task) is CalendarState.INACTIVE:
            done = f"[{scope_label(task.task, task.occurrence_status)}] "
        repeat = "반복 · " if task.occurrence_start is not None else ""
        attachment = " · 첨부" if task.has_attachments else ""
        snoozed_until = self._snoozed_until_for_task(task)
        snooze = (
            f" · ⏰ 재알림 {format_snoozed_time(snoozed_until, self._timezone)}"
            if snoozed_until is not None
            else ""
        )
        return f"{time_label}  ·  {repeat}{pin}{done}{task.title}{attachment}{snooze}"

    def _snoozed_until_for_task(self, task: ScheduledTask) -> datetime | None:
        if task.id is None:
            return None
        occurrence_start = task.occurrence_start or task.starts_at
        exact = self._snoozed_until.get((task.id, occurrence_start))
        if exact is not None:
            return exact
        without_occurrence = self._snoozed_until.get((task.id, None))
        if without_occurrence is not None:
            return without_occurrence
        if task.occurrence_start is not None:
            return None
        candidates = (
            due for (task_id, _occurrence), due in self._snoozed_until.items() if task_id == task.id
        )
        return min(candidates, default=None)


def _days_in_month(year: int, month: int) -> int:
    next_year, next_month = shift_month(year, month, 1)
    return (date(next_year, next_month, 1) - timedelta(days=1)).day
