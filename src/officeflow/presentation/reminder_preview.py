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
from PySide6.QtGui import QColor, QPainter
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

from officeflow.application.reminder_preview import (
    ReminderPreviewItem,
    ReminderPreviewResult,
    ReminderPreviewService,
)
from officeflow.presentation.background import OperationWorker, finish_thread
from officeflow.presentation.dates import format_datetime


def preview_text(item: ReminderPreviewItem, timezone: str) -> str:
    attachment = " · 첨부" if item.task.has_attachments else ""
    return f"{format_datetime(item.effective_at, timezone, short=True)}  {item.task.title}{attachment} · {item.status}"


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
        item = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(item, ReminderPreviewItem):
            super().paint(painter, option, index)
            return
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
        status = {"시각 지남 · 발생 기록 없음": "발생 기록 없음"}.get(item.status, item.status)
        state_width = min(metrics.horizontalAdvance(status) + 8, int(rect.width() * 0.4))
        time_width = metrics.horizontalAdvance("00:00") + 14
        painter.drawText(
            QRect(rect.left(), rect.top(), time_width, rect.height()),
            Qt.AlignmentFlag.AlignVCenter,
            format_datetime(item.effective_at, self._timezone).rsplit(" ", 1)[1],
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
            "[알림] " if opt.widget.objectName() == "calendarDayList" else ""
        ) + item.task.title
        if item.task.has_attachments:
            title += " · 첨부"
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
        self._result = ReminderPreviewResult((), True)
        self._dialog: QDialog | None = None
        self._max_rows = 3
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        heading = QHBoxLayout()
        self.label = QLabel("오늘 알림 확인 중…")
        heading.addWidget(self.label, 1)
        self.more = QPushButton("전체 보기")
        self.more.clicked.connect(self._open_all)
        heading.addWidget(self.more)
        layout.addLayout(heading)
        self.list = QListWidget()
        self.list.setObjectName("reminderPreviewList")
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
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        widget.customContextMenuRequested.connect(lambda point: self._menu(widget, point))

    def _menu(self, widget: QListWidget, point: QPoint) -> None:
        item = widget.itemAt(point)
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(widget)
        menu.addAction("업무 기록·첨부파일 보기").triggered.connect(
            lambda: self.recordRequested.emit(data)
        )
        menu.addAction("알림 설정…").triggered.connect(
            lambda: self.settingsRequested.emit(data.task)
        )
        menu.exec(widget.viewport().mapToGlobal(point))

    def set_result(self, result: ReminderPreviewResult) -> None:
        self._result = result
        pending = tuple(i for i in result.items if i.is_pending)
        visible = (pending or result.items)[: self._max_rows]
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
            f"오늘 알림 · {count} · {len(visible)}건 표시"
            if result.items
            else "오늘 예약된 알림이 없습니다"
            if result.complete
            else "알림 조회가 끝나지 않았습니다 · 다시 확인 필요"
        )
        self.label.setWordWrap(True)
        hidden = len(result.items) - len(visible)
        self.more.setText(f"{hidden}건 더 보기" if hidden else "전체 보기")
        if not result.complete:
            self.more.setText("더 조회…")
        if self._dialog:
            full = self._dialog.findChild(QListWidget)
            if full:
                self._fill(full, result.items)

    def set_compact(self, compact: bool) -> None:
        rows = 2 if compact else 3
        if rows != self._max_rows:
            self._max_rows = rows
            self.set_result(self._result)

    def shutdown(self) -> None:
        if self._dialog:
            self._dialog.reject()

    def _fill(self, widget: QListWidget, items: tuple[ReminderPreviewItem, ...]) -> None:
        selected = (
            widget.currentItem().data(Qt.ItemDataRole.UserRole).key
            if widget.currentItem()
            else None
        )
        scroll = widget.verticalScrollBar().value()
        widget.clear()
        for item in items:
            row = QListWidgetItem(preview_text(item, self._timezone))
            row.setSizeHint(QSize(0, max(32, widget.fontMetrics().height() + 12)))
            row.setData(Qt.ItemDataRole.UserRole, item)
            if not item.is_pending:
                row.setForeground(QColor("#7D899E"))
            schedule = (
                format_datetime(item.task.starts_at, self._timezone, short=True)
                if item.task.starts_at
                else "없음"
            )
            row.setToolTip(
                f"{preview_text(item, self._timezone)}\n업무 일정: {schedule}\n예정 알림: {format_datetime(item.scheduled_at, self._timezone)}"
            )
            if item.snoozed_until:
                row.setToolTip(
                    row.toolTip()
                    + "\n다시 알림: "
                    + format_datetime(item.snoozed_until, self._timezone)
                )
            if item.actual_fired_at:
                row.setToolTip(
                    row.toolTip()
                    + "\n최근 발생: "
                    + format_datetime(item.actual_fired_at, self._timezone)
                )
            widget.addItem(row)
            if item.key == selected:
                widget.setCurrentItem(row)
        widget.verticalScrollBar().setValue(scroll)

    def _open_all(self) -> None:
        if not self._result.complete:
            self.reloadRequested.emit()
        if self._dialog:
            self._dialog.raise_()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("오늘 알림 · 전체 보기")
        dialog.resize(650, 430)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("더블클릭: 업무 기록 · 오른쪽 클릭: 알림 설정"))
        full = QListWidget()
        full.setObjectName("reminderPreviewList")
        self._connect_list(full)
        layout.addWidget(full, 1)
        self._fill(full, self._result.items)
        self._dialog = dialog

        def finished(_result: int) -> None:
            self._dialog = None
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.show()
