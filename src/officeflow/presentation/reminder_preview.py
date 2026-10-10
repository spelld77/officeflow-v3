from __future__ import annotations

from datetime import date, datetime

from PySide6.QtCore import (
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QSize,
    Qt,
    QThread,
    Signal,
    Slot,
)
from PySide6.QtGui import QCloseEvent, QColor, QPainter
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from officeflow.application.reminder_groups import ReminderGroup, group_reminders
from officeflow.application.reminder_preview import (
    ReminderPreviewItem,
    ReminderPreviewResult,
    ReminderPreviewService,
)
from officeflow.presentation.background import OperationWorker, finish_thread
from officeflow.presentation.dates import format_datetime
from officeflow.presentation.lookup_style import SCROLLBAR_STYLE
from officeflow.presentation.reminder_groups import (
    ReminderGroupDetailsDialog,
    group_summary,
    group_text,
    group_tooltip,
)
from officeflow.presentation.theme import LIGHT_STYLESHEET


def preview_text(item: ReminderPreviewItem, timezone: str) -> str:
    attachment = " · 첨부" if item.task.has_attachments else ""
    return f"{format_datetime(item.effective_at, timezone, short=True)}  {item.task.title}{attachment} · {item.display_status}"


class ReminderPreviewDelegate(QStyledItemDelegate):
    def __init__(self, timezone: str, parent: QObject) -> None:
        super().__init__(parent)
        self._timezone = timezone

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        data = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, (ReminderPreviewItem, ReminderGroup)):
            super().paint(painter, option, index)
            return
        item = data.representative if isinstance(data, ReminderGroup) else data
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        assert opt.widget is not None
        opt.widget.style().drawControl(
            QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget
        )
        painter.save()
        painter.setPen(opt.palette.text().color())
        metrics = opt.fontMetrics
        rect = opt.rect.adjusted(8, 0, -8, 0)
        status = (
            group_summary(data, self._timezone)
            if isinstance(data, ReminderGroup)
            else {"시각 지남 · 발생 기록 없음": "발생 기록 없음"}.get(
                item.display_status, item.display_status
            )
        )
        state_width = min(metrics.horizontalAdvance(status) + 8, int(rect.width() * 0.4))
        time_width = metrics.horizontalAdvance("00:00") + 14
        time_label = format_datetime(item.effective_at, self._timezone).rsplit(" ", 1)[1]
        if isinstance(data, ReminderGroup) and data.scheduled:
            time_label = (
                "종일"
                if data.scheduled.all_day
                else format_datetime(data.scheduled.starts_at, self._timezone).rsplit(" ", 1)[1]
            )
        painter.drawText(
            QRect(rect.left(), rect.top(), time_width, rect.height()),
            Qt.AlignmentFlag.AlignVCenter,
            time_label,
        )
        state_rect = QRect(rect.right() - state_width, rect.top(), state_width, rect.height())
        painter.drawText(
            state_rect,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(status, Qt.TextElideMode.ElideRight, state_width),
        )
        title_rect = QRect(
            rect.left() + time_width,
            rect.top(),
            max(1, rect.width() - time_width - state_width - 8),
            rect.height(),
        )
        title = (
            "[알림만] "
            if isinstance(data, ReminderGroup)
            and not data.scheduled
            and opt.widget.objectName() == "calendarDayList"
            else ""
        ) + (data.task.title if isinstance(data, ReminderGroup) else item.task.title)
        if isinstance(data, ReminderGroup) and data.occurrence_start:
            title = (
                "반복 "
                + format_datetime(data.occurrence_start, self._timezone, short=True)
                + " · "
                + title
            )
        elif isinstance(data, ReminderGroup) and data.task.recurrence_rule:
            title = "공통 알림 · " + title
        if (data.task if isinstance(data, ReminderGroup) else item.task).has_attachments:
            title = "첨부 · " + title
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width()),
        )
        painter.restore()


class ReminderPreviewController(QObject):
    ready = Signal(object)
    rangeReady = Signal(object, object)
    failed = Signal(object)

    def __init__(self, service: ReminderPreviewService, parent: QObject) -> None:
        super().__init__(parent)
        self._service = service
        self._thread: QThread | None = None
        self._worker: OperationWorker | None = None
        self._generation = 0
        self._active_request: tuple[int, date, date] | None = None
        self._request: tuple[int, tuple[date, date, str, datetime, int, float], datetime] | None = (
            None
        )
        self._closing = False

    def request(
        self,
        start: date,
        end: date,
        timezone: str,
        now: datetime,
        *,
        limit: int = 5000,
        seconds: float = 2.0,
    ) -> None:
        key = (start, end, timezone, now.replace(second=0, microsecond=0), limit, seconds)
        self._generation += 1
        self._request = (self._generation, key, now)
        if self._worker:
            self._worker.cancel()
        else:
            self._start()

    def _start(self) -> None:
        if self._closing or self._request is None:
            return
        generation, key, now = self._request
        self._request = None
        start, end, timezone, _minute, limit, seconds = key
        thread = QThread(self)
        worker = OperationWorker(
            lambda canceled: self._service.query(
                start,
                end,
                timezone=timezone,
                now=now,
                limit=limit,
                seconds=seconds,
                cancel_requested=canceled,
            )
        )
        self._thread, self._worker = thread, worker
        self._active_request = (generation, start, end)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # Plain Python closures run in the sender's worker thread in PySide.
        # Context-bound queued slots keep all UI-facing signals and lifecycle
        # updates on this controller's (GUI) thread, including failure paths.
        worker.succeeded.connect(self._on_succeeded, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        thread.start()

    @Slot(object)
    def _on_succeeded(self, result: ReminderPreviewResult) -> None:
        active = self._active_request
        if active is not None and active[0] == self._generation and not self._closing:
            self.ready.emit(result)
            self.rangeReady.emit((active[1], active[2]), result)

    @Slot(object)
    def _on_failed(self, error: object) -> None:
        active = self._active_request
        if active is not None and active[0] == self._generation and not self._closing:
            self.failed.emit(error)

    @Slot()
    def _on_finished(self) -> None:
        thread = self._thread
        # finished is emitted before native-thread/deferred-delete cleanup has
        # fully returned. Keep the Python worker owner until the thread joins.
        if thread is not None:
            thread.wait()
        self._thread = None
        self._worker = None
        self._active_request = None
        if thread is not None:
            thread.deleteLater()
        self._start()

    def shutdown(self) -> None:
        self._closing = True
        self._request = None
        if self._worker:
            self._worker.cancel()
        thread = self._thread
        if thread:
            finish_thread(thread)
            if isValid(thread):
                thread.wait()


class ReminderPreviewWidget(QWidget):
    recordRequested = Signal(object)
    settingsRequested = Signal(object)
    selected = Signal(object)
    reloadRequested = Signal()

    def __init__(self, timezone: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._timezone = timezone
        self.list: QListWidget
        self._result = ReminderPreviewResult((), True)
        self._dialog: QDialog | None = None
        self._details: ReminderGroupDetailsDialog | None = None
        self._groups: tuple[ReminderGroup, ...] = ()
        self._all_page = 0
        self._all_previous: QPushButton | None = None
        self._all_next: QPushButton | None = None
        self._max_rows = 3
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        heading = QHBoxLayout()
        self.label = QLabel("오늘 알림 확인 중…")
        heading.addWidget(self.label, 1)
        self.details_button = QPushButton("알림 시각 보기")
        self.details_button.setEnabled(False)
        self.details_button.clicked.connect(lambda: self._show_details(self.list))
        heading.addWidget(self.details_button)
        self.more = QPushButton("전체 보기")
        self.more.clicked.connect(self._open_all)
        heading.addWidget(self.more)
        layout.addLayout(heading)
        self.list = QListWidget()
        self.list.setObjectName("reminderPreviewList")
        self.list.setStyleSheet(SCROLLBAR_STYLE)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setMaximumHeight(110)
        self.list.setMinimumHeight(32)
        self._connect_list(self.list)
        layout.addWidget(self.list)
        self.hint = QLabel("알림은 업무 검색·필터와 무관하게 표시됩니다.")
        self.hint.setObjectName("mutedText")
        layout.addWidget(self.hint)

    def _connect_list(self, widget: QListWidget) -> None:
        widget.setItemDelegate(ReminderPreviewDelegate(self._timezone, widget))
        widget.itemDoubleClicked.connect(
            lambda item: self.recordRequested.emit(item.data(Qt.ItemDataRole.UserRole))
        )
        widget.currentItemChanged.connect(
            lambda item, _previous: (
                self.selected.emit(item.data(Qt.ItemDataRole.UserRole)) if item else None
            )
        )
        if widget is self.list:
            widget.currentItemChanged.connect(
                lambda item, _previous: self.details_button.setEnabled(item is not None)
            )
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        widget.customContextMenuRequested.connect(lambda point: self._menu(widget, point))

    def _menu(self, widget: QListWidget, point: QPoint) -> None:
        item = widget.itemAt(point)
        if item is None:
            return
        widget.setCurrentItem(item)
        data = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(widget)
        menu.setStyleSheet(LIGHT_STYLESHEET)
        menu.addAction("알림 시각·상태 보기").triggered.connect(lambda: self._show_details(widget))
        menu.addAction("업무 기록·첨부파일 보기").triggered.connect(
            lambda: self.recordRequested.emit(data)
        )
        menu.addAction("알림 설정…").triggered.connect(
            lambda: self.settingsRequested.emit(data.task)
        )
        menu.exec(widget.viewport().mapToGlobal(point))

    def set_result(self, result: ReminderPreviewResult) -> None:
        self._result = result
        self._groups = group_reminders(result.items)
        pending = tuple(i for i in result.items if i.is_pending)
        attention = tuple(i for i in result.items if i.needs_attention)
        history = len(result.items) - len(pending) - len(attention)
        active_groups = tuple(g for g in self._groups if g.pending_count or g.attention_count)
        visible = (active_groups or self._groups)[: self._max_rows]
        self._fill(self.list, visible)
        row_height = max(32, self.list.fontMetrics().height() + 12)
        self.list.setFixedHeight(len(visible) * row_height + 10 if visible else 32)
        self.list.setVisible(bool(visible))
        self.more.setVisible(bool(result.items) or not result.complete)
        count = (
            f"총 {len(result.items)}건"
            if result.complete
            else f"{len(result.items)}건 이상 · 일부 조회"
        )
        self.label.setText(
            f"오늘 알림 · 업무 {len(self._groups)}개 · {count} · {len(visible)}개 표시"
            if result.items
            else "오늘 예약된 알림이 없습니다"
            if result.complete
            else "알림 조회가 끝나지 않았습니다 · 다시 확인 필요"
        )
        self.label.setToolTip(
            f"확인할/예정 {len(pending)}건 · 처리 이력 {history}건 · 발생 기록 확인 필요 {len(attention)}건"
        )
        self.label.setWordWrap(True)
        hidden = len(self._groups) - len(visible)
        self.more.setText(f"{hidden}개 더 보기" if hidden else "전체 보기")
        if not result.complete:
            self.more.setText("더 조회…")
        self._refresh_all()
        if self._details:
            key = self._details.group_key
            self._details.set_group(
                next((g for g in self._groups if g.key == key), None), complete=result.complete
            )

    def set_compact(self, compact: bool) -> None:
        rows = 2 if compact else 3
        if rows != self._max_rows:
            self._max_rows = rows
            self.set_result(self._result)

    def shutdown(self) -> None:
        if self._dialog:
            self._dialog.reject()
        if self._details:
            self._details.reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)

    def _fill(self, widget: QListWidget, items: tuple[ReminderGroup, ...]) -> None:
        selected = (
            widget.currentItem().data(Qt.ItemDataRole.UserRole).key
            if widget.currentItem()
            else None
        )
        scroll = widget.verticalScrollBar().value()
        widget.clear()
        for item in items:
            row = QListWidgetItem(group_text(item, self._timezone))
            row.setSizeHint(QSize(0, max(32, widget.fontMetrics().height() + 12)))
            row.setData(Qt.ItemDataRole.UserRole, item)
            if not item.pending_count and not item.attention_count:
                row.setForeground(QColor("#7D899E"))
            row.setToolTip(group_tooltip(item, self._timezone))
            widget.addItem(row)
            if item.key == selected:
                widget.setCurrentItem(row)
        widget.verticalScrollBar().setValue(scroll)

    def _show_details(self, widget: QListWidget) -> None:
        row = widget.currentItem()
        if row is None:
            return
        group = row.data(Qt.ItemDataRole.UserRole)
        if not isinstance(group, ReminderGroup):
            return
        if self._details:
            self._details.set_group(group, complete=self._result.complete)
            self._details.show()
            self._details.raise_()
            return
        dialog = ReminderGroupDetailsDialog(
            group, self._timezone, complete=self._result.complete, parent=self
        )
        dialog.recordRequested.connect(self.recordRequested.emit)
        dialog.settingsRequested.connect(self.settingsRequested.emit)
        self._details = dialog

        def finished(_result: int) -> None:
            self._details = None
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.show()

    def _refresh_all(self) -> None:
        if self._dialog:
            full = self._dialog.findChild(QListWidget)
            self._all_page = min(self._all_page, max(0, (len(self._groups) - 1) // 100))
            if full:
                self._fill(full, self._groups[self._all_page * 100 : (self._all_page + 1) * 100])
            assert self._all_previous is not None and self._all_next is not None
            self._all_previous.setVisible(len(self._groups) > 100)
            self._all_next.setVisible(len(self._groups) > 100)
            self._all_previous.setEnabled(self._all_page > 0)
            self._all_next.setEnabled((self._all_page + 1) * 100 < len(self._groups))
            label = self._dialog.findChild(QLabel, "reminderGroupPage")
            if label:
                label.setText(
                    f"업무 {len(self._groups)}개{' 이상 · 일부 조회' if not self._result.complete else ''} · {self._all_page + 1}페이지"
                )

    def _move_all(self, delta: int) -> None:
        self._all_page += delta
        self._refresh_all()

    def _open_all(self) -> None:
        if not self._result.complete:
            self.reloadRequested.emit()
        if self._dialog:
            self._dialog.raise_()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("오늘 알림 · 전체 보기")
        dialog.setStyleSheet(LIGHT_STYLESHEET + SCROLLBAR_STYLE)
        dialog.setMinimumSize(460, 340)
        dialog.resize(650, 430)
        layout = QVBoxLayout(dialog)
        description = QLabel("더블클릭: 업무 기록 · 오른쪽 클릭: 알림 시각·상태")
        description.setWordWrap(True)
        layout.addWidget(description)
        full = QListWidget()
        full.setObjectName("reminderPreviewList")
        self._connect_list(full)
        layout.addWidget(full, 1)
        full.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        paging = QHBoxLayout()
        self._all_previous = QPushButton("이전")
        self._all_next = QPushButton("다음")
        self._all_previous.clicked.connect(lambda: self._move_all(-1))
        self._all_next.clicked.connect(lambda: self._move_all(1))
        page_label = QLabel()
        page_label.setObjectName("reminderGroupPage")
        paging.addWidget(self._all_previous)
        paging.addWidget(page_label, 1)
        paging.addWidget(self._all_next)
        layout.addLayout(paging)
        actions = QHBoxLayout()
        details = QPushButton("알림 시각 보기")
        details.clicked.connect(lambda: self._show_details(full))
        close = QPushButton("닫기")
        close.clicked.connect(dialog.reject)
        actions.addWidget(details)
        actions.addWidget(close)
        layout.addLayout(actions)
        self._dialog = dialog
        self._all_page = 0
        self._refresh_all()

        def finished(_result: int) -> None:
            self._dialog = None
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.show()
