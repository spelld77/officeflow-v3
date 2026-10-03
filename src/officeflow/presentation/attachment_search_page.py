from __future__ import annotations

from datetime import date, datetime, time, timedelta
from threading import Event
from zoneinfo import ZoneInfo

from PySide6.QtCore import (
    QAbstractListModel,
    QDate,
    QModelIndex,
    QPersistentModelIndex,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.attachment_search import (
    AttachmentCursor,
    AttachmentKind,
    AttachmentSearchHit,
    AttachmentSearchInterrupted,
    AttachmentSearchPage,
    AttachmentSearchQuery,
    AttachmentSearchService,
)
from officeflow.domain.enums import TaskStatus
from officeflow.presentation.background import finish_thread

STATUS_LABELS = {
    TaskStatus.ACTIVE: "진행",
    TaskStatus.PENDING: "대기",
    TaskStatus.COMPLETED: "완료",
    TaskStatus.CANCELED: "취소",
    TaskStatus.ARCHIVED: "보관",
}


class SearchWorker(QThread):
    completed = Signal(int, object, bool)
    failed = Signal(int, str)

    def __init__(
        self,
        service: AttachmentSearchService,
        query: AttachmentSearchQuery,
        generation: int,
        append: bool,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._query = query
        self._generation = generation
        self._append = append
        self.cancelled = Event()

    def run(self) -> None:
        try:
            page = self._service.search_page(self._query, cancel_requested=self.cancelled.is_set)
            self.completed.emit(self._generation, page, self._append)
        except AttachmentSearchInterrupted as error:
            self.failed.emit(self._generation, str(error))
        except Exception:
            self.failed.emit(
                self._generation, "파일 검색을 완료하지 못했습니다. 다시 검색해 주세요."
            )


class AttachmentSearchModel(QAbstractListModel):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.items: list[AttachmentSearchHit] = []

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.items)

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        hit = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return hit.original_name
        if role == Qt.ItemDataRole.UserRole:
            return hit
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{hit.original_name}\n{hit.task_title}\n{STATUS_LABELS[hit.task_status]}"
        return None

    def set_items(self, items: list[AttachmentSearchHit]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()


class AttachmentSearchDelegate(QStyledItemDelegate):
    def __init__(self, timezone: str, parent: QWidget) -> None:
        super().__init__(parent)
        self._zone = ZoneInfo(timezone)

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return QSize(100, max(64, option.fontMetrics.height() * 2 + 22))

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        from PySide6.QtWidgets import QStyle

        hit = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(hit, AttachmentSearchHit):
            return
        painter.save()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.fillRect(option.rect, QColor("#EAF1FF" if selected else "#FFFFFF"))
        rect = option.rect.adjusted(10, 5, -10, -5)
        painter.setFont(option.font)
        painter.setPen(QColor("#14213D"))
        painter.drawText(
            rect.adjusted(0, 0, 0, -rect.height() // 2),
            Qt.AlignmentFlag.AlignVCenter,
            option.fontMetrics.elidedText(
                hit.original_name, Qt.TextElideMode.ElideRight, rect.width()
            ),
        )
        flags = [STATUS_LABELS[hit.task_status]]
        if hit.deleted:
            flags.append("휴지통")
        if hit.detached:
            flags.append("정리 대기")
        if hit.missing:
            flags.append("파일 누락")
        size = (
            f"{hit.size_bytes / 1024:.0f} KB"
            if hit.size_bytes < 1024**2
            else f"{hit.size_bytes / 1024**2:.1f} MB"
        )
        meta = f"{hit.task_title} · {'/'.join(flags)} · {hit.created_at.astimezone(self._zone):%m/%d} 첨부 · {size}"
        painter.setPen(QColor("#58657D"))
        painter.drawText(
            rect.adjusted(0, rect.height() // 2, 0, 0),
            Qt.AlignmentFlag.AlignVCenter,
            option.fontMetrics.elidedText(meta, Qt.TextElideMode.ElideRight, rect.width()),
        )
        painter.restore()


class AttachmentSearchPageWidget(QFrame):
    openRequested = Signal(object)
    taskRequested = Signal(object)
    manageRequested = Signal(object)
    backRequested = Signal()
    clearRequested = Signal()
    MAX_ROWS = 500

    def __init__(
        self, service: AttachmentSearchService, timezone: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("contentCard")
        self._service = service
        self._timezone = timezone
        self._search = ""
        self._active = False
        self._generation = 0
        self._worker: SearchWorker | None = None
        self._pending: tuple[AttachmentSearchQuery, bool] | None = None
        self._cursor: AttachmentCursor | None = None
        self._has_more = False
        self._block_start = 0
        self._block_cursors: list[AttachmentCursor | None] = [None]
        self._selected_id: int | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.refresh)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        heading = QHBoxLayout()
        title = QLabel("첨부파일 찾기")
        title.setObjectName("pageTitle")
        heading.addWidget(title)
        heading.addStretch()
        self.back_button = QPushButton("업무로 돌아가기")
        self.back_button.clicked.connect(self.backRequested)
        heading.addWidget(self.back_button)
        root.addLayout(heading)
        self.scope_label = QLabel("전체 기간 · 완료/보관 포함 · 파일명만 검색")
        self.scope_label.setWordWrap(True)
        root.addWidget(self.scope_label)
        filters = QHBoxLayout()
        self.kind_combo = QComboBox()
        for label, kind in (
            ("파일 종류: 전체", AttachmentKind.ALL),
            ("Excel", AttachmentKind.EXCEL),
            ("PDF", AttachmentKind.PDF),
            ("Word", AttachmentKind.WORD),
            ("이미지", AttachmentKind.IMAGE),
            ("기타", AttachmentKind.OTHER),
        ):
            self.kind_combo.addItem(label, kind.value)
        self.kind_combo.currentIndexChanged.connect(self._conditions_changed)
        filters.addWidget(self.kind_combo)
        self.more_conditions = QPushButton("추가 조건")
        self.more_conditions.setCheckable(True)
        filters.addWidget(self.more_conditions)
        filters.addStretch()
        self.clear_button = QPushButton("검색 해제")
        self.clear_button.clicked.connect(self.clearRequested)
        filters.addWidget(self.clear_button)
        root.addLayout(filters)
        self.conditions = QWidget()
        conditions = QVBoxLayout(self.conditions)
        conditions.setContentsMargins(0, 0, 0, 0)
        dates = QHBoxLayout()
        self.range_check = QCheckBox("첨부일")
        dates.addWidget(self.range_check)
        self.date_from = QDateEdit(QDate.currentDate().addMonths(-1))
        self.date_to = QDateEdit(QDate.currentDate())
        for edit in (self.date_from, self.date_to):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
            dates.addWidget(edit)
            edit.dateChanged.connect(self._conditions_changed)
        conditions.addLayout(dates)
        states = QHBoxLayout()
        self.trash_check = QCheckBox("휴지통 업무 포함")
        self.detached_check = QCheckBox("정리 대기 파일 포함")
        states.addWidget(self.trash_check)
        states.addWidget(self.detached_check)
        states.addStretch()
        conditions.addLayout(states)
        for check in (self.range_check, self.trash_check, self.detached_check):
            check.toggled.connect(self._conditions_changed)
        root.addWidget(self.conditions)
        self.conditions.hide()
        self.more_conditions.toggled.connect(self.conditions.setVisible)
        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        self.feedback.setObjectName("taskSearchFeedbackLabel")
        root.addWidget(self.feedback)
        self.model = AttachmentSearchModel(self)
        self.list_view = QListView()
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(AttachmentSearchDelegate(timezone, self.list_view))
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.selectionModel().currentChanged.connect(self._selection_changed)
        self.model.modelReset.connect(lambda: self._selection_changed(QModelIndex(), QModelIndex()))
        self.list_view.doubleClicked.connect(lambda _index: self._emit(self.openRequested))
        self._enter = QShortcut(QKeySequence("Return"), self.list_view)
        self._enter.setContext(Qt.ShortcutContext.WidgetShortcut)
        self._enter.activated.connect(lambda: self._emit(self.openRequested))
        root.addWidget(self.list_view, 1)
        paging = QHBoxLayout()
        self.previous_button = QPushButton("이전 구간")
        self.next_button = QPushButton("더 보기")
        self.previous_button.clicked.connect(self._previous)
        self.next_button.clicked.connect(self._next)
        paging.addWidget(self.previous_button)
        paging.addStretch()
        paging.addWidget(self.next_button)
        root.addLayout(paging)
        self.selection_label = QLabel("파일을 선택해 주세요.")
        self.selection_label.setWordWrap(True)
        root.addWidget(self.selection_label)
        actions = QHBoxLayout()
        self.open_button = QPushButton("파일 열기")
        self.task_button = QPushButton("연결 업무")
        self.manage_button = QPushButton("첨부 관리")
        for button, signal in (
            (self.open_button, self.openRequested),
            (self.task_button, self.taskRequested),
            (self.manage_button, self.manageRequested),
        ):
            button.setEnabled(False)
            button.clicked.connect(lambda _checked=False, s=signal: self._emit(s))
            actions.addWidget(button)
        actions.addStretch()
        root.addLayout(actions)
        self._conditions_changed()

    def _emit(self, signal: object) -> None:
        hit = self.selected_hit()
        if hit is not None:
            signal.emit(hit)  # type: ignore[attr-defined]

    def selected_hit(self) -> AttachmentSearchHit | None:
        hit = self.list_view.currentIndex().data(Qt.ItemDataRole.UserRole)
        return hit if isinstance(hit, AttachmentSearchHit) else None

    def _selection_changed(self, _current: QModelIndex, _previous: QModelIndex) -> None:
        hit = self.selected_hit()
        self._selected_id = hit.attachment_id if hit else None
        self.selection_label.setText(
            f"선택: {hit.original_name}" if hit else "파일을 선택해 주세요."
        )
        for button in (self.open_button, self.task_button, self.manage_button):
            button.setEnabled(hit is not None)
        self.manage_button.setText("첨부 정리" if hit and hit.detached else "첨부 관리")

    def activate(self, search: str, *, calendar: bool = False) -> None:
        self._active = False
        self.reset_conditions()
        self.back_button.setText("캘린더로 돌아가기" if calendar else "업무로 돌아가기")
        self._active = True
        self.set_search(search)

    def deactivate(self) -> None:
        self._active = False
        self._generation += 1
        self._timer.stop()
        self._pending = None
        if self._worker is not None:
            self._worker.cancelled.set()

    def reset_conditions(self) -> None:
        self.kind_combo.setCurrentIndex(0)
        for check in (self.range_check, self.trash_check, self.detached_check):
            check.setChecked(False)
        self.more_conditions.setChecked(False)
        self._conditions_changed()

    def set_search(self, search: str) -> None:
        self._search = search.strip()
        self._generation += 1
        self._pending = None
        if self._worker is not None:
            self._worker.cancelled.set()
        self.model.set_items([])
        self.next_button.setEnabled(False)
        self.previous_button.setEnabled(False)
        self.feedback.setText(
            f"“{self._search}” 파일명 검색 준비 중…" if self._search else "최근 첨부파일 조회 중…"
        )
        if self._active:
            self._timer.start()

    def _conditions_changed(self, _value: object = None) -> None:
        enabled = self.range_check.isChecked()
        self.date_from.setEnabled(enabled)
        self.date_to.setEnabled(enabled)
        count = sum(
            (
                enabled,
                self.trash_check.isChecked(),
                self.detached_check.isChecked(),
                self.kind_combo.currentIndex() != 0,
            )
        )
        self.more_conditions.setText(f"추가 조건 ({count})" if count else "추가 조건")
        scope = (
            f"첨부일 {self.date_from.date().toString('yyyy-MM-dd')}~{self.date_to.date().toString('yyyy-MM-dd')}"
            if enabled
            else "전체 기간"
        )
        self.scope_label.setText(
            scope
            + " · 완료/보관 포함 · 파일명만 검색"
            + (" · 휴지통 포함" if self.trash_check.isChecked() else "")
            + (" · 정리 대기 포함" if self.detached_check.isChecked() else "")
        )
        if self._active:
            self.set_search(self._search)

    def _query(self, cursor: AttachmentCursor | None = None) -> AttachmentSearchQuery:
        zone = ZoneInfo(self._timezone)

        def boundary(day: QDate) -> datetime:
            return datetime.combine(date(day.year(), day.month(), day.day()), time.min, tzinfo=zone)

        return AttachmentSearchQuery(
            search=self._search,
            kind=AttachmentKind(self.kind_combo.currentData()),
            attached_after=boundary(self.date_from.date())
            if self.range_check.isChecked()
            else None,
            attached_before=boundary(self.date_to.date()) + timedelta(days=1)
            if self.range_check.isChecked()
            else None,
            include_trash=self.trash_check.isChecked(),
            include_detached=self.detached_check.isChecked(),
            cursor=cursor,
        )

    def refresh(self) -> None:
        if not self._active:
            return
        self._generation += 1
        self._block_start = 0
        self._block_cursors = [None]
        self._request(None, False)

    def _request(self, cursor: AttachmentCursor | None, append: bool) -> None:
        self._timer.stop()
        try:
            query = self._query(cursor)
        except ValueError as error:
            self.model.set_items([])
            self.feedback.setText(str(error))
            self._pending = None
            self.next_button.setEnabled(False)
            self.previous_button.setEnabled(False)
            return
        self._pending = (query, append)
        self.next_button.setEnabled(False)
        self.previous_button.setEnabled(False)
        if self._worker is not None:
            self._worker.cancelled.set()
        else:
            self._start_pending()

    def _start_pending(self) -> None:
        if self._pending is None or not self._active:
            return
        query, append = self._pending
        self._pending = None
        worker = SearchWorker(self._service, query, self._generation, append, self)
        self._worker = worker
        worker.completed.connect(self._receive)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._finished)
        worker.start()

    def _finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
        self._worker = None
        self._start_pending()

    def _receive(self, generation: int, page: AttachmentSearchPage, append: bool) -> None:
        if generation != self._generation or not self._active:
            return
        selected_id = self._selected_id
        items = (self.model.items + list(page.items)) if append else list(page.items)
        self.model.set_items(items)
        self._cursor, self._has_more = page.next_cursor, page.has_more
        self.next_button.setText("다음 구간" if len(items) >= self.MAX_ROWS else "더 보기")
        self.next_button.setEnabled(page.has_more)
        self.previous_button.setEnabled(self._block_start > 0)
        label = f"“{self._search}” 파일명 검색 중" if self._search else "최근 첨부파일"
        if items:
            count = (
                f"{self._block_start + 1}~{self._block_start + len(items)}개 표시"
                if page.has_more or self._block_start
                else f"{len(items)}개 파일"
            )
            self.feedback.setText(f"{label} · {count}" + (" · 더 있음" if page.has_more else ""))
        else:
            self.feedback.setText(
                f"{label} · 결과가 없습니다. 검색어 또는 추가 조건을 확인해 주세요."
            )
        for row, hit in enumerate(items):
            if hit.attachment_id == selected_id:
                self.list_view.setCurrentIndex(self.model.index(row, 0))
                break

    def _failed(self, generation: int, message: str) -> None:
        if generation == self._generation and self._active:
            self.feedback.setText(message)
            self.next_button.setEnabled(False)
            self.previous_button.setEnabled(self._block_start > 0)

    def _next(self) -> None:
        if not self._has_more:
            return
        append = len(self.model.items) < self.MAX_ROWS
        if not append:
            self._block_start += len(self.model.items)
            self._block_cursors.append(self._cursor)
            self.model.set_items([])
        self._request(self._cursor, append)

    def _previous(self) -> None:
        if self._block_start <= 0:
            return
        self._block_start = max(0, self._block_start - self.MAX_ROWS)
        self._block_cursors.pop()
        self.model.set_items([])
        self._request(self._block_cursors[-1], False)

    def shutdown(self) -> None:
        self.deactivate()
        if self._worker is not None:
            self._worker.cancelled.set()
            finish_thread(self._worker, parent=self, label="첨부 검색을 마무리하고 있습니다…")
