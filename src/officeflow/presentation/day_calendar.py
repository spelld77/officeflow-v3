from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QHelpEvent, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.day_display import (
    DayMark,
    DayValue,
    day_alarms,
    day_bounds,
    day_marks,
    value_scope,
)
from officeflow.application.day_overview import DayCursor, DayOverviewResult
from officeflow.application.reminder_preview import ReminderPreviewItem, ReminderPreviewResult
from officeflow.application.tasks import ScheduledTask
from officeflow.domain.enums import TaskStatus
from officeflow.presentation.calendar_style import (
    calendar_color,
    day_status,
    priority_icon,
    priority_label,
)
from officeflow.presentation.dates import format_date, format_datetime
from officeflow.presentation.theme import LIGHT_STYLESHEET


def value_text(value: DayValue, timezone: str) -> str:
    def exact(stamp: datetime) -> str:
        local = stamp.astimezone(ZoneInfo(timezone))
        repeated = local.utcoffset() != local.replace(fold=1 - local.fold).utcoffset()
        return format_datetime(stamp, timezone) + (
            " UTC" + local.strftime("%z") if repeated else ""
        )

    if isinstance(value, ReminderPreviewItem):
        return f"{exact(value.effective_at)} · {value.task.title} · {value.display_status}"
    label = "종일" if value.all_day else exact(value.starts_at)
    end = " → " + exact(value.ends_at) if value.ends_at and not value.all_day else ""
    return (
        label
        + end
        + " · "
        + value.title
        + " · "
        + day_status(value)
        + " · "
        + priority_label(value.priority)
    )


class DayEntriesList(QWidget):
    selected = Signal(object)
    activated = Signal(object)
    contextRequested = Signal(object, object)
    moreRequested = Signal()

    def __init__(self, timezone: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.timezone = timezone
        self.entries: tuple[DayValue, ...] = ()
        self.page = 0
        self.complete = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget(self)
        self.list.setObjectName("dayOverviewList")
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.currentItemChanged.connect(
            lambda current, _previous: (
                self.selected.emit(current.data(Qt.ItemDataRole.UserRole)) if current else None
            )
        )
        self.list.itemActivated.connect(
            lambda item: self.activated.emit(item.data(Qt.ItemDataRole.UserRole))
        )
        self.list.customContextMenuRequested.connect(self._context)
        layout.addWidget(self.list, 1)
        footer = QHBoxLayout()
        self.previous = QPushButton("이전", self)
        self.next = QPushButton("다음", self)
        self.count = QLabel(self)
        footer.addWidget(self.previous)
        footer.addWidget(self.count, 1)
        footer.addWidget(self.next)
        layout.addLayout(footer)
        self.previous.clicked.connect(lambda: self._move(-1))
        self.next.clicked.connect(lambda: self._move(1))

    def set_entries(self, values: tuple[DayValue, ...], *, complete: bool = True) -> None:
        selected = self.list.currentItem()
        key = self._key(selected.data(Qt.ItemDataRole.UserRole)) if selected else None
        scroll = self.list.verticalScrollBar().value()
        self.entries = values
        self.complete = complete
        self.page = min(self.page, max(0, (len(values) - 1) // 100))
        self._fill(key)
        self.list.verticalScrollBar().setValue(scroll)

    @staticmethod
    def _key(value: DayValue) -> object:
        return value.key if isinstance(value, ReminderPreviewItem) else value_scope(value)

    def _fill(self, key: object = None) -> None:
        self.list.clear()
        for value in self.entries[self.page * 100 : (self.page + 1) * 100]:
            text = value_text(value, self.timezone)
            item = QListWidgetItem(text, self.list)
            item.setToolTip(text)
            item.setData(Qt.ItemDataRole.UserRole, value)
            if isinstance(value, ScheduledTask):
                item.setIcon(priority_icon(value))
                if value.status in {TaskStatus.COMPLETED, TaskStatus.CANCELED, TaskStatus.ARCHIVED}:
                    item.setForeground(QColor("#6d7788"))
            if key is not None and self._key(value) == key:
                self.list.setCurrentItem(item)
        self.previous.setEnabled(self.page > 0)
        self.next.setEnabled((self.page + 1) * 100 < len(self.entries) or not self.complete)
        self.count.setText(
            f"조회된 {len(self.entries)}건 · {self.page + 1}페이지"
            + (" · 일부 조회" if not self.complete else "")
        )

    def _move(self, delta: int) -> None:
        target = self.page + delta
        if target * 100 >= len(self.entries) and not self.complete:
            self.moreRequested.emit()
            return
        if 0 <= target <= max(0, (len(self.entries) - 1) // 100):
            self.page = target
            self._fill()

    def _context(self, point: QPoint) -> None:
        item = self.list.itemAt(point)
        if item:
            self.list.setCurrentItem(item)
            self.contextRequested.emit(
                item.data(Qt.ItemDataRole.UserRole), self.list.viewport().mapToGlobal(point)
            )


class DayTimeline(QWidget):
    focusMoved = Signal(int)
    selected = Signal(object)
    activated = Signal(object)
    contextRequested = Signal(object, object)
    groupRequested = Signal(object)
    HOUR = 56
    AXIS = 56

    def __init__(self, timezone: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.timezone = timezone
        self.day = datetime.now(ZoneInfo(timezone)).date()
        self.clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.marks: tuple[DayMark, ...] = ()
        self.hits: list[tuple[QRectF, DayMark]] = []
        self.selection: object = None
        self._focused: DayMark | None = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumHeight(24 * self.HOUR + 60)
        self.setAccessibleName(
            "하루 시간표. 위아래 키로 항목 이동, Enter로 업무 보기, 메뉴 키로 업무 메뉴"
        )

    def set_values(
        self,
        day: date,
        schedules: tuple[ScheduledTask, ...],
        alarms: tuple[ReminderPreviewItem, ...],
    ) -> None:
        self.day = day
        lower, upper = day_bounds(day, self.timezone)
        self.setMinimumHeight(round((upper - lower).total_seconds() / 3600 * self.HOUR) + 60)
        self.AXIS = 90 if (upper - lower).total_seconds() != 86400 else 56
        self.marks = day_marks(day, self.timezone, schedules, alarms)
        self._focused = next(
            (
                mark
                for mark in self.marks
                if any(value_scope(value) == self.selection for value in mark.values)
            ),
            None,
        )
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = max(1, self.width() - self.AXIS - 12)
        middle = self.AXIS + width * 0.56
        painter.setPen(QPen(QColor("#e1e7f1"), 1))
        lower, upper = day_bounds(self.day, self.timezone)
        hours = round((upper - lower).total_seconds() / 3600)
        for hour in range(hours + 1):
            stamp = (lower + timedelta(hours=hour)).astimezone(ZoneInfo(self.timezone))
            repeated = stamp.utcoffset() != stamp.replace(fold=1 - stamp.fold).utcoffset()
            label = stamp.strftime("%H:%M") + (stamp.strftime(" %z") if repeated else "")
            y = 12 + hour * self.HOUR
            if event.rect().top() - self.HOUR <= y <= event.rect().bottom() + self.HOUR:
                painter.drawLine(self.AXIS, y, self.width() - 6, y)
                painter.setPen(QColor("#63738c"))
                painter.drawText(
                    QRectF(0, y - 8, self.AXIS - 8, 20),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    "24:00" if hour == hours else label,
                )
                painter.setPen(QPen(QColor("#f0f3f8"), 1))
                painter.drawLine(
                    self.AXIS, y + self.HOUR // 2, self.width() - 6, y + self.HOUR // 2
                )
                painter.setPen(QPen(QColor("#e1e7f1"), 1))
        painter.drawLine(QPoint(round(middle), 0), QPoint(round(middle), self.height()))
        self.hits = []
        for mark in self.marks:
            rect = self.mark_rect(mark)
            self.hits.append((rect, mark))
            if not rect.intersects(QRectF(event.rect())):
                continue
            alarm = mark.column == "alarm"
            single = len(mark.values) == 1
            value = mark.values[0]
            closed = (
                single
                and isinstance(value, ScheduledTask)
                and value.status
                in {
                    TaskStatus.COMPLETED,
                    TaskStatus.CANCELED,
                    TaskStatus.ARCHIVED,
                }
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#f0f2f5" if closed else "#fff3df" if alarm else "#eaf1ff"))
            painter.drawRoundedRect(rect, 5, 5)
            if single and isinstance(value, ScheduledTask):
                painter.setBrush(QColor(calendar_color(value)))
                painter.drawRoundedRect(
                    QRectF(rect.left() + 2, rect.top() + 4, 4, rect.height() - 8), 2, 2
                )
            selected = any(value_scope(value) == self.selection for value in mark.values)
            painter.setPen(
                QPen(
                    QColor("#316df3" if selected else "#b5c9ef" if not alarm else "#ebca97"),
                    2 if selected else 1,
                )
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, 5, 5)
            painter.setPen(QColor("#6d7788" if closed else "#172033"))
            if not single:
                label = f"알림 {len(mark.values)}건" if alarm else f"+{len(mark.values)} 일정"
            else:
                label = value.task.title
                if (
                    alarm
                    or (isinstance(value, ScheduledTask) and value.ends_at is None)
                    or rect.height() < 43
                ):
                    stamp = (
                        value.effective_at
                        if isinstance(value, ReminderPreviewItem)
                        else value.starts_at
                    )
                    label = (
                        stamp.astimezone(ZoneInfo(self.timezone)).strftime("%H:%M") + " · " + label
                    )
            text_rect = rect.adjusted(11 if not alarm and single else 7, 3, -5, -3)
            metrics = painter.fontMetrics()
            title_width = text_rect.width()
            if single:
                status = day_status(value)
                badge_width = min(text_rect.width(), metrics.horizontalAdvance(status) + 10)
                title_width = max(0.0, text_rect.width() - badge_width - 5)
                if title_width < 25:
                    badge_width, title_width = text_rect.width(), 0
                badge = QRectF(text_rect.right() - badge_width, text_rect.top(), badge_width, 20)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#e1e6ec" if closed else "#ffffff"))
                painter.drawRoundedRect(badge, 4, 4)
                painter.setPen(QColor("#526078"))
                painter.drawText(
                    badge.adjusted(3, 0, -3, 0),
                    Qt.AlignmentFlag.AlignCenter,
                    metrics.elidedText(
                        status, Qt.TextElideMode.ElideRight, max(1, round(badge.width() - 6))
                    ),
                )
            painter.setPen(QColor("#6d7788" if closed else "#172033"))
            if title_width:
                painter.drawText(
                    QRectF(text_rect.left(), text_rect.top(), title_width, 20),
                    Qt.AlignmentFlag.AlignVCenter,
                    metrics.elidedText(
                        label, Qt.TextElideMode.ElideRight, max(1, round(title_width))
                    ),
                )
            if rect.height() >= 43:
                start = (
                    value.effective_at
                    if isinstance(value, ReminderPreviewItem)
                    else value.starts_at
                )
                caption = start.astimezone(ZoneInfo(self.timezone)).strftime("%H:%M")
                if isinstance(value, ScheduledTask) and value.ends_at:
                    caption += "-" + value.ends_at.astimezone(ZoneInfo(self.timezone)).strftime(
                        "%H:%M"
                    )
                painter.drawText(
                    QRectF(text_rect.left(), text_rect.top() + 20, text_rect.width(), 18),
                    Qt.AlignmentFlag.AlignVCenter,
                    metrics.elidedText(
                        caption, Qt.TextElideMode.ElideRight, max(1, round(text_rect.width()))
                    ),
                )
        now = self.clock().astimezone(ZoneInfo(self.timezone))
        if now.date() == self.day:
            now_y = 12 + (now.astimezone(UTC) - lower).total_seconds() / 3600 * self.HOUR
            painter.setPen(QPen(QColor("#d04d49"), 1))
            painter.drawLine(self.AXIS, round(now_y), self.width() - 6, round(now_y))

    def mark_rect(self, mark: DayMark) -> QRectF:
        """Use identical visual bounds for painting and off-screen navigation."""
        width = max(1, self.width() - self.AXIS - 12)
        middle = self.AXIS + width * 0.56
        left = self.AXIS + 4 if mark.column == "schedule" else middle + 7
        area = middle - self.AXIS - 10 if mark.column == "schedule" else self.width() - middle - 14
        cell = area / mark.lanes
        return QRectF(
            left + mark.lane * cell,
            12 + mark.minute * self.HOUR / 60,
            max(1, cell - 4),
            max(32, (mark.bottom - mark.minute) * self.HOUR / 60 - 3),
        )

    def _hit(self, point: QPoint) -> DayMark | None:
        return next((mark for rect, mark in reversed(self.hits) if rect.contains(point)), None)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        mark = self._hit(event.position().toPoint())
        if mark:
            self._focused = mark
            if len(mark.values) > 1:
                self.groupRequested.emit(mark.values)
            else:
                value = mark.values[0]
                self.selection = value_scope(value)
                self.selected.emit(value)
                if event.button() == Qt.MouseButton.RightButton:
                    self.contextRequested.emit(value, event.globalPosition().toPoint())
            self.update()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        mark = self._hit(event.position().toPoint())
        if mark and len(mark.values) == 1:
            self.activated.emit(mark.values[0])

    def event(self, event: QEvent) -> bool:
        if isinstance(event, QHelpEvent):
            mark = self._hit(event.pos())
            if mark:
                QToolTip.showText(
                    event.globalPos(),
                    "\n".join(value_text(value, self.timezone) for value in mark.values[:20])
                    + ("\n나머지는 목록에서 확인" if len(mark.values) > 20 else ""),
                    self,
                )
                return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        ordered = sorted(self.marks, key=lambda mark: (mark.minute, mark.column, mark.lane))
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down) and ordered:
            index = ordered.index(self._focused) if self._focused in ordered else -1
            self._focused = ordered[
                (index + (1 if event.key() == Qt.Key.Key_Down else -1)) % len(ordered)
            ]
            self.selection = value_scope(self._focused.values[0])
            self.selected.emit(self._focused.values[0])
            self.update()
            self.focusMoved.emit(round(12 + self._focused.minute * self.HOUR / 60))
            event.accept()
            return
        if self._focused and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if len(self._focused.values) > 1:
                self.groupRequested.emit(self._focused.values)
            else:
                self.activated.emit(self._focused.values[0])
            return
        if self._focused and (
            event.key() == Qt.Key.Key_Menu
            or (
                event.key() == Qt.Key.Key_F10
                and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            )
        ):
            self.contextRequested.emit(self._focused.values[0], self.mapToGlobal(QPoint(60, 30)))
            return
        super().keyPressEvent(event)


class DayCalendarPanel(QWidget):
    selectionCleared = Signal()
    scheduleCursorRequested = Signal(object)
    selected = Signal(object)
    activated = Signal(object)
    contextRequested = Signal(object, object)
    scheduleMoreRequested = Signal()
    alarmMoreRequested = Signal()

    def __init__(self, timezone: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.timezone = timezone
        self.day = datetime.now(ZoneInfo(timezone)).date()
        self.result = DayOverviewResult(self.day, (), False)
        self.reminders = ReminderPreviewResult((), False)
        self._schedule_loaded = False
        self._alarm_loaded = False
        self.mode = "day"
        self._positioned: date | None = None
        self._dialogs: list[QDialog] = []
        self._page_cursors: list[DayCursor | None] = [None]
        self._page_index = 0
        self._selected_scope: object = None
        self._selected_key: object = None
        self._errors: dict[str, str] = {}
        self._closed = False
        self._position_target = 0
        self._position_timer = QTimer(self)
        self._position_timer.setSingleShot(True)
        self._position_timer.timeout.connect(self._scroll_initial)
        self._edge_timer = QTimer(self)
        self._edge_timer.setSingleShot(True)
        self._edge_timer.timeout.connect(self._update_edges)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(5)
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.summary)
        self.notice = QLabel(self)
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet("color:#89540d;")
        self.notice.hide()
        root.addWidget(self.notice)
        self.stack = QStackedWidget(self)
        root.addWidget(self.stack, 1)
        timeline_page = QWidget(self)
        body = QVBoxLayout(timeline_page)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(4)
        self.all_day = QListWidget(self)
        self.all_day.setObjectName("dayOverviewList")
        self.all_day.setIconSize(QSize(14, 18))
        self.all_day.installEventFilter(self)
        self.all_day.itemClicked.connect(
            lambda item: self.selected.emit(item.data(Qt.ItemDataRole.UserRole))
        )
        self.all_day.itemDoubleClicked.connect(
            lambda item: self.activated.emit(item.data(Qt.ItemDataRole.UserRole))
        )
        self.all_day.itemActivated.connect(
            lambda item: self.activated.emit(item.data(Qt.ItemDataRole.UserRole))
        )
        self.all_day.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.all_day.customContextMenuRequested.connect(self._all_day_context)
        body.addWidget(self.all_day)
        self.all_day_more = QPushButton(self)
        self.all_day_more.clicked.connect(
            lambda: self._show_group(tuple(item for item in self.result.items if item.all_day))
        )
        body.addWidget(self.all_day_more)
        labels = QHBoxLayout()
        self.column_labels = labels
        labels.setContentsMargins(56, 0, 8, 0)
        schedule_label = QLabel("업무 일정", self)
        schedule_label.setToolTip(
            "중요도: 보통 파랑 · 관심 초록 · 중요 주황 · 긴급 빨강. 완료·취소·보관은 회색입니다."
        )
        labels.addWidget(schedule_label, 56)
        labels.addWidget(QLabel("알림", self), 44)
        body.addLayout(labels)
        navigation = QHBoxLayout()
        navigation.setSpacing(6)
        self.earlier_button = QPushButton("↑ 이전 없음", self)
        self.later_button = QPushButton("↓ 이후 없음", self)
        self.now_button = QPushButton("지금", self)
        self.now_button.setToolTip("오늘의 현재 시각으로 이동")
        for button in (self.earlier_button, self.later_button, self.now_button):
            button.setProperty("dayTimeAction", True)
            button.setAutoDefault(False)
            navigation.addWidget(button)
        navigation.addStretch()
        body.addLayout(navigation)
        self.earlier_button.clicked.connect(lambda: self._jump_edge(-1))
        self.later_button.clicked.connect(lambda: self._jump_edge(1))
        self.now_button.clicked.connect(self._jump_now)
        self.time_scroll = QScrollArea(self)
        self.time_scroll.setObjectName("dayTimelineScroll")
        self.time_scroll.setWidgetResizable(True)
        self.time_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.timeline = DayTimeline(timezone, self)
        self.time_scroll.setWidget(self.timeline)
        self.time_scroll.viewport().installEventFilter(self)
        self.timeline.installEventFilter(self)
        bar = self.time_scroll.verticalScrollBar()
        bar.installEventFilter(self)
        bar.valueChanged.connect(self._queue_edges)
        bar.rangeChanged.connect(self._queue_edges)
        bar.actionTriggered.connect(self._keep_scroll_position)
        bar.sliderPressed.connect(self._keep_scroll_position)
        self.timeline.focusMoved.connect(lambda y: self.time_scroll.ensureVisible(0, y, 0, 50))
        body.addWidget(self.time_scroll, 1)
        self.stack.addWidget(timeline_page)
        list_page = QWidget(self)
        list_layout = QVBoxLayout(list_page)
        list_layout.setContentsMargins(0, 0, 0, 0)
        self.history = QCheckBox("처리 이력 보기", self)
        self.history.toggled.connect(self._refresh)
        list_layout.addWidget(self.history)
        self.entries = DayEntriesList(timezone, self)
        list_layout.addWidget(self.entries, 1)
        self.stack.addWidget(list_page)
        footer = QHBoxLayout()
        self.schedule_list_button = QPushButton("일정 목록", self)
        self.schedule_list_button.clicked.connect(self._toggle_schedule_list)
        self.previous_schedule = QPushButton("이전 일정 조회", self)
        self.previous_schedule.clicked.connect(
            lambda: self.scheduleCursorRequested.emit(self._page_cursors[self._page_index - 1])
        )
        self.more_schedule = QPushButton("다음 일정 조회", self)
        self.more_schedule.clicked.connect(self.scheduleMoreRequested.emit)
        self.more_alarm = QPushButton("알림 더 조회", self)
        self.more_alarm.clicked.connect(self.alarmMoreRequested.emit)
        for button in (
            self.schedule_list_button,
            self.previous_schedule,
            self.more_schedule,
            self.more_alarm,
        ):
            footer.addWidget(button)
        footer.addStretch()
        root.addLayout(footer)
        self.detail = QLabel(self)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setWordWrap(False)
        self.detail.setMinimumWidth(0)
        self.detail.setMaximumHeight(24)
        root.addWidget(self.detail)
        self.detail.hide()
        self.timeline.groupRequested.connect(self._show_group)
        for source in (self.timeline, self.entries):
            source.selected.connect(self._select)
            source.activated.connect(self.activated.emit)
            source.contextRequested.connect(self.contextRequested.emit)
        self.entries.moreRequested.connect(
            lambda: (
                self.alarmMoreRequested.emit()
                if self.mode == "alarms"
                else self.scheduleMoreRequested.emit()
            )
        )
        self._timer = QTimer(self)
        self._timer.setInterval(60_000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.stack.setCurrentIndex(0 if mode == "day" else 1)
        self.history.setVisible(mode == "alarms")
        self._refresh()

    def _toggle_schedule_list(self) -> None:
        self.set_mode("day" if self.mode == "schedule-list" else "schedule-list")
        if self.mode == "day" and self._page_index:
            self.scheduleCursorRequested.emit(None)

    def set_day(self, day: date) -> None:
        if self.day != day:
            self.day = day
            self._position_timer.stop()
            self._positioned = None
            self.result = DayOverviewResult(day, (), False)
            self.reminders = ReminderPreviewResult((), False)
            self._schedule_loaded = self._alarm_loaded = False
            self.entries.page = 0
            self._page_cursors = [None]
            self._page_index = 0
            self._selected_scope = None
            self._selected_key = None
            self.timeline.selection = None
            self.detail.clear()
            self.notice.clear()
            self.notice.hide()
            self._errors.clear()
            self._refresh()

    def set_result(self, result: DayOverviewResult) -> None:
        if result.day != self.day:
            return
        if result.requested_cursor != self.result.requested_cursor:
            self.entries.page = 0
        if result.requested_cursor is None:
            self._page_cursors = [None]
            self._page_index = 0
        elif result.requested_cursor in self._page_cursors:
            self._page_index = self._page_cursors.index(result.requested_cursor)
        else:
            self._page_cursors = [
                *self._page_cursors[: self._page_index + 1],
                result.requested_cursor,
            ]
            self._page_index += 1
        self.result = result
        self._schedule_loaded = True
        self._errors.pop("schedule", None)
        self._show_errors()
        self._refresh()

    def set_reminders(self, result: ReminderPreviewResult) -> None:
        self._alarm_loaded = True
        self._errors.pop("alarm", None)
        self._show_errors()
        self.reminders = ReminderPreviewResult(
            result.for_date(self.day, self.timezone), result.complete
        )
        self._refresh()

    def set_error(self, error: object, *, source: str = "schedule") -> None:
        self._errors[source] = str(error)
        self._show_errors()

    def _show_errors(self) -> None:
        self.notice.setText(
            " · ".join(
                f"{'알림' if source == 'alarm' else '일정'} 갱신 실패 · 이전 결과: {message}"
                for source, message in self._errors.items()
            )
        )
        self.notice.setVisible(bool(self._errors))

    def _refresh(self) -> None:
        alarms = day_alarms(self.reminders.items)
        pending = tuple(item for item in alarms if item.is_pending or item.needs_attention)
        history = tuple(item for item in alarms if not (item.is_pending or item.needs_attention))
        unhandled = sum(item.is_pending and item.status == "미확인" for item in pending)
        attention = sum(item.needs_attention for item in pending)
        normal = len(pending) - unhandled - attention
        scope = (
            f"일정 검색 결과 {len(self.result.items)}개 · 알림은 하루 전체"
            if self.result.search
            else f"{'일정' if self.result.complete else '조회된 일정'} {len(self.result.items)}개"
        )
        alarm_summary = (
            f"예정·재알림 {normal} · 미확인 {unhandled} · 확인 필요 {attention}"
            if self._alarm_loaded
            else "알림 조회 중"
        )
        self.summary.setText(
            f"{scope if self._schedule_loaded else '일정 조회 중'} · {alarm_summary}"
            + (
                " · 일부 조회"
                if (self._schedule_loaded and not self.result.complete)
                or (self._alarm_loaded and not self.reminders.complete)
                else ""
            )
        )
        self.history.setText(f"처리 이력 {len(history)}건 보기")
        self.more_schedule.setVisible(self.mode != "alarms" and not self.result.complete)
        self.previous_schedule.setVisible(self.mode == "schedule-list" and self._page_index > 0)
        self.more_alarm.setVisible(not self.reminders.complete)
        self.schedule_list_button.setVisible(self.mode != "alarms")
        self.schedule_list_button.setText("시간표" if self.mode == "schedule-list" else "일정 목록")
        self.entries.set_entries(
            cast(
                tuple[DayValue, ...],
                tuple(
                    sorted(
                        (*pending, *(history if self.history.isChecked() else ())),
                        key=lambda i: i.effective_at,
                    )
                )
                if self.mode == "alarms"
                else self.result.items,
            ),
            complete=self.reminders.complete if self.mode == "alarms" else self.result.complete,
        )
        self.timeline.set_values(self.day, self.result.items, alarms)
        self.column_labels.setContentsMargins(self.timeline.AXIS, 0, 8, 0)
        current_values: dict[object, DayValue] = {
            DayEntriesList._key(value): value for value in self.result.items
        }
        current_values.update((DayEntriesList._key(alarm), alarm) for alarm in alarms)
        if self._selected_key is not None:
            selected = current_values.get(self._selected_key)
            if selected is not None:
                self._select(selected)
            else:
                self._selected_scope = self._selected_key = None
                self.timeline.selection = None
                self.detail.hide()
                self.selectionCleared.emit()
        all_day = tuple(item for item in self.result.items if item.all_day)
        self.all_day.clear()
        for value in all_day[:2]:
            item = QListWidgetItem(
                "종일 · " + day_status(value) + " · " + value.title, self.all_day
            )
            item.setIcon(priority_icon(value))
            if value.status in {TaskStatus.COMPLETED, TaskStatus.CANCELED, TaskStatus.ARCHIVED}:
                item.setForeground(QColor("#6d7788"))
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setToolTip(value_text(value, self.timezone))
        self.all_day.setVisible(bool(all_day))
        self._fit_all_day_rows()
        self.all_day_more.setVisible(len(all_day) > 2)
        self.all_day_more.setText(f"종일 +{max(0, len(all_day) - 2)}개")
        self._queue_edges()
        if self._positioned is None and (self._schedule_loaded or self._alarm_loaded):
            earliest = min((mark.minute for mark in self.timeline.marks), default=540)
            self._position_target = round(earliest * DayTimeline.HOUR / 60)
            self._position_timer.start(0)

    def _scroll_initial(self) -> None:
        if self._closed or self.mode != "day":
            return
        self.time_scroll.verticalScrollBar().setValue(self._position_target)
        if (self._schedule_loaded or "schedule" in self._errors) and (
            self._alarm_loaded or "alarm" in self._errors
        ):
            self._positioned = self.day
        self._queue_edges()

    def _fit_all_day_rows(self) -> None:
        self.all_day.ensurePolished()
        height = max(34, self.all_day.fontMetrics().height() + 16)
        for row in range(self.all_day.count()):
            self.all_day.item(row).setSizeHint(QSize(0, height))
        margins = self.all_day.contentsMargins()
        self.all_day.setFixedHeight(
            max(
                8,
                self.all_day.count() * (height + 2 * self.all_day.spacing())
                + margins.top()
                + margins.bottom(),
            )
        )

    def _keep_scroll_position(self, *_args: object) -> None:
        self._positioned = self.day
        self._position_timer.stop()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.all_day and event.type() in {
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
        }:
            self._fit_all_day_rows()
        if event.type() in {
            QEvent.Type.Wheel,
            QEvent.Type.KeyPress,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.TouchBegin,
        }:
            self._keep_scroll_position()
        if event.type() in {QEvent.Type.Resize, QEvent.Type.Show}:
            self._queue_edges()
        return super().eventFilter(watched, event)

    def _tick(self) -> None:
        self.timeline.update()
        self._queue_edges()

    def _queue_edges(self, *_args: object) -> None:
        if not self._closed and not self._edge_timer.isActive():
            self._edge_timer.start(0)

    def _outside(self, direction: int) -> tuple[int, int, float | None]:
        top = self.time_scroll.verticalScrollBar().value()
        bottom = top + self.time_scroll.viewport().height()
        schedules = alarms = 0
        targets: list[tuple[float, float]] = []
        for mark in self.timeline.marks:
            rect = self.timeline.mark_rect(mark)
            if direction < 0 and rect.bottom() <= top:
                targets.append((rect.bottom(), rect.top()))
            elif direction > 0 and rect.top() >= bottom:
                targets.append((rect.top(), rect.top()))
            else:
                continue
            if mark.column == "schedule":
                schedules += len(mark.values)
            else:
                alarms += len(mark.values)
        target = (max(targets) if direction < 0 else min(targets))[1] if targets else None
        return schedules, alarms, target

    def _update_edges(self) -> None:
        self.now_button.setVisible(
            self.timeline.clock().astimezone(ZoneInfo(self.timezone)).date() == self.day
        )
        for direction, button, arrow, label in (
            (-1, self.earlier_button, "↑", "이전"),
            (1, self.later_button, "↓", "이후"),
        ):
            schedules, alarms, target = self._outside(direction)
            count = schedules + alarms
            button.setText(f"{arrow} {label} {str(count) + '건' if count else '없음'}")
            button.setEnabled(target is not None)
            scope = (
                " · 조회된 항목 기준"
                if not (self.result.complete and self.reminders.complete)
                else ""
            )
            button.setToolTip(f"화면 {label} 일정 {schedules}개 · 알림 {alarms}건{scope}")
            button.setAccessibleName(button.text() + " · " + button.toolTip())

    def _jump_edge(self, direction: int) -> None:
        self._keep_scroll_position()
        _schedules, _alarms, target = self._outside(direction)
        if target is not None:
            self.time_scroll.verticalScrollBar().setValue(max(0, round(target) - 12))
        self._queue_edges()

    def _jump_now(self) -> None:
        now = self.timeline.clock().astimezone(ZoneInfo(self.timezone))
        if now.date() != self.day:
            self._queue_edges()
            return
        self._keep_scroll_position()
        elapsed = (now.astimezone(UTC) - day_bounds(self.day, self.timezone)[0]).total_seconds()
        self.time_scroll.verticalScrollBar().setValue(
            max(0, round((elapsed / 3600 - 1) * DayTimeline.HOUR))
        )
        self._queue_edges()

    def _select(self, value: DayValue) -> None:
        self._selected_scope = value_scope(value)
        self._selected_key = DayEntriesList._key(value)
        text = value_text(value, self.timezone)
        self.detail.setText(
            self.detail.fontMetrics().elidedText(
                text, Qt.TextElideMode.ElideRight, max(100, self.width() - 10)
            )
        )
        self.detail.setToolTip(text)
        self.detail.show()
        self.timeline.selection = value_scope(value)
        self.timeline.update()
        self.selected.emit(value)

    def _all_day_context(self, point: QPoint) -> None:
        item = self.all_day.itemAt(point)
        if item:
            self.contextRequested.emit(
                item.data(Qt.ItemDataRole.UserRole), self.all_day.viewport().mapToGlobal(point)
            )

    def _show_group(self, values: tuple[DayValue, ...]) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(format_date(self.day) + " · 전체 항목")
        dialog.setStyleSheet(LIGHT_STYLESHEET)
        dialog.resize(560, 360)
        layout = QVBoxLayout(dialog)
        entries = DayEntriesList(self.timezone, dialog)
        entries.set_entries(values)
        entries.selected.connect(self._select)
        entries.activated.connect(self.activated.emit)
        entries.contextRequested.connect(self.contextRequested.emit)
        layout.addWidget(entries)
        close = QPushButton("닫기", dialog)
        close.clicked.connect(dialog.close)
        layout.addWidget(close)
        self._dialogs.append(dialog)

        def finished(_result: int) -> None:
            self._dialogs.remove(dialog)
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.show()

    def shutdown(self) -> None:
        self._closed = True
        self._position_timer.stop()
        self._edge_timer.stop()
        self._timer.stop()
        for dialog in tuple(self._dialogs):
            dialog.close()
