from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from datetime import datetime

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.completed_browse import (
    CompletedBrowseRepository,
    CompletedEntry,
    CompletedEntryKey,
    CompletionCursor,
    CompletionRequest,
    CompletionResult,
    Month,
)
from officeflow.application.tasks import TaskQuery
from officeflow.presentation.dates import format_datetime
from officeflow.presentation.latest_job import LatestJob
from officeflow.presentation.theme import LIGHT_STYLESHEET


class CompletedBrowseWidget(QWidget):
    openRequested = Signal(object)
    selected = Signal(object)
    menuRequested = Signal(object, object)
    modeChanged = Signal(str)
    totalChanged = Signal(int)

    def __init__(
        self, repository: CompletedBrowseRepository, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.setStyleSheet(LIGHT_STYLESHEET)
        self._jobs = LatestJob(self)
        self._jobs.ready.connect(self._ready)
        self._jobs.failed.connect(self._failed)
        self._request: CompletionRequest | None = None
        self.result: CompletionResult | None = None
        self._expanded: OrderedDict[Month, None] = OrderedDict()
        self._cursors: dict[Month, CompletionCursor | None] = {}
        self._previous: dict[Month, list[CompletionCursor | None]] = {}
        self._page_number: dict[Month, int] = {}
        self._before: datetime | None = None
        self._rendering = False
        self._dirty = False
        self._initialized = False
        self._collapsed_all = False
        self._saved_selection: tuple[CompletedEntryKey, ...] = ()
        self._saved_scroll = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        self.mode = QComboBox(self)
        self.mode.addItem("월별 묶음", "months")
        self.mode.addItem("전체 목록", "flat")
        self.mode.currentIndexChanged.connect(self._change_mode)
        toolbar.addWidget(self.mode)
        toolbar.addStretch()
        self.recent = QPushButton("최근 기간", self)
        self.recent.clicked.connect(self._latest)
        toolbar.addWidget(self.recent)
        self.older = QPushButton("더 과거", self)
        self.older.clicked.connect(self._older)
        toolbar.addWidget(self.older)
        layout.addLayout(toolbar)
        self.status = QLabel("완료한 날짜 기준", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.tree = QTreeWidget(self)
        self.tree.setStyleSheet("""
            QTreeWidget, QTreeWidget QWidget#qt_scrollarea_viewport {
                background: #FFFFFF; color: #172033;
                selection-background-color: #DFE9FF; selection-color: #172033;
            }
            QTreeWidget::item { padding: 8px 4px; }
            QTreeWidget::item:selected { background: #DFE9FF; color: #172033; }
            QScrollBar:vertical { background: #EDF1F7; width: 13px; margin: 0; }
            QScrollBar::handle:vertical { background: #7FA3EF; min-height: 28px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        """)
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(1)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.itemExpanded.connect(lambda item: self._toggle(item, True))
        self.tree.itemCollapsed.connect(lambda item: self._toggle(item, False))
        self.tree.itemActivated.connect(self._activate)
        self.tree.itemSelectionChanged.connect(self._select)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        layout.addWidget(self.tree, 1)
        self.hide()

    @property
    def flat(self) -> bool:
        return bool(self.mode.currentData() == "flat")

    @property
    def needs_refresh(self) -> bool:
        return self._dirty

    def _failed(self, _error: str) -> None:
        self.status.setText("완료 기록을 불러오지 못했습니다. 다시 시도해 주세요.")

    def set_mode(self, value: str) -> None:
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(1 if value == "flat" else 0)
        self.mode.blockSignals(False)

    def _change_mode(self) -> None:
        self.modeChanged.emit(str(self.mode.currentData()))
        self._reset()
        self.refresh()

    def _reset(self) -> None:
        self._initialized = False
        self._collapsed_all = False
        self._saved_selection = ()
        self._saved_scroll = 0
        self._expanded.clear()
        self._cursors.clear()
        self._previous.clear()
        self._page_number.clear()

    def refresh(self, query: TaskQuery | None = None, timezone: str | None = None) -> None:
        if query is not None:
            normalized = replace(query, offset=0, limit=50)
            if (
                self._request is None
                or self._request.query != normalized
                or self._request.timezone != timezone
            ):
                self._reset()
                self._before = None
                self._request = CompletionRequest(normalized, timezone or "Asia/Seoul")
        if self._request is None:
            return
        request = replace(
            self._request,
            before=self._before,
            flat=self.flat,
            expanded=tuple(self._expanded) if self._initialized else None,
            cursors=tuple(self._cursors.items()),
        )
        # Explicitly collapsed all groups must remain collapsed.
        if getattr(self, "_collapsed_all", False):
            request = replace(request, expanded=())
        self._dirty = False
        self.status.setText("완료 기록 조회 중…")
        self._jobs.submit(lambda: self.repository.read(request))

    def _ready(self, value: object) -> None:
        if not isinstance(value, CompletionResult):
            return
        if not self.isVisible():
            self._dirty = True
            return
        selected = self.selected_keys() or self._saved_selection
        scroll = (
            self.tree.verticalScrollBar().value()
            if self.tree.topLevelItemCount()
            else self._saved_scroll
        )
        self.result = value
        self._initialized = True
        self._rendering = True
        self.tree.clear()
        visible_keys = {page.month for page in value.pages}
        self._expanded = OrderedDict((key, None) for key in self._expanded if key in visible_keys)
        for loaded_page in value.pages:
            self._expanded.setdefault(loaded_page.month, None)
        pages = {page.month: page for page in value.pages}
        for bucket in value.buckets:
            label = (
                "완료 기록"
                if self.flat
                else f"{bucket.month[0]}년 {bucket.month[1]}월"
                if bucket.month
                else "완료일 미상"
            )
            group = QTreeWidgetItem(self.tree, [f"{label} · {bucket.count:,}건"])
            group.setData(0, Qt.ItemDataRole.UserRole, ("group", bucket.month))
            group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            group.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
            page = pages.get(bucket.month)
            if page is None:
                continue
            for entry in page.entries:
                when = (
                    format_datetime(entry.completed_at, self._request.timezone)
                    if entry.completed_at and self._request
                    else "완료일 미상"
                )
                scope = (
                    f" · 회차 {format_datetime(entry.key.original_occurrence_start, self._request.timezone)}"
                    if entry.key.original_occurrence_start and self._request
                    else " · 업무 전체 완료"
                    if entry.recurrence
                    else ""
                )
                item = QTreeWidgetItem(group, [entry.title + "\n" + when + scope])
                item.setData(0, Qt.ItemDataRole.UserRole, entry)
                item.setToolTip(0, item.text(0))
                if entry.key in selected:
                    item.setSelected(True)
            number = self._page_number.get(bucket.month, 0)
            if bucket.count > 50:
                row = QTreeWidgetItem(group)
                row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                controls = QWidget(self.tree)
                bar = QHBoxLayout(controls)
                bar.setContentsMargins(0, 0, 0, 0)
                previous = QPushButton("이전 50건", controls)
                previous.setEnabled(bool(self._previous.get(bucket.month)))
                previous.clicked.connect(
                    lambda _checked=False, key=bucket.month: self._previous_page(key)
                )
                bar.addWidget(previous)
                bar.addWidget(
                    QLabel(
                        f"{number * 50 + 1}-{number * 50 + len(page.entries)} / {bucket.count}",
                        controls,
                    )
                )
                next_button = QPushButton("다음 50건", controls)
                next_button.setEnabled(page.next_cursor is not None)
                next_button.clicked.connect(
                    lambda _checked=False, key=bucket.month, cursor=page.next_cursor: (
                        self._next_page(key, cursor)
                    )
                )
                bar.addWidget(next_button)
                first = QPushButton("처음", controls)
                first.clicked.connect(
                    lambda _checked=False, key=bucket.month: self._first_page(key)
                )
                bar.addWidget(first)
                self.tree.setItemWidget(row, 0, controls)
            group.setExpanded(True)
        self.tree.verticalScrollBar().setValue(scroll)
        self._rendering = False
        self.older.setEnabled(value.has_older and not self.flat)
        self.recent.setVisible(not self.flat)
        self.older.setVisible(not self.flat)
        self.status.setText(
            f"완료한 날짜 기준 · 전체 {value.total:,}건"
            + (f" · 이 기간 {value.period_count:,}건" if not self.flat else "")
            + (" · 조건에 맞는 완료 기록 없음" if not value.total else "")
        )
        self.totalChanged.emit(value.total)
        self._select()

    def _toggle(self, item: QTreeWidgetItem, expanded: bool) -> None:
        if self._rendering:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(data, tuple) or data[0] != "group":
            return
        key = data[1]
        if expanded:
            self._expanded.pop(key, None)
            self._expanded[key] = None
            if len(self._expanded) > 2:
                old, _ = self._expanded.popitem(last=False)
                self._cursors.pop(old, None)
                self._previous.pop(old, None)
                self._page_number.pop(old, None)
        else:
            self._expanded.pop(key, None)
            self._cursors.pop(key, None)
            self._previous.pop(key, None)
            self._page_number.pop(key, None)
        self._collapsed_all = not self._expanded
        self.refresh()

    def selected_keys(self) -> tuple[CompletedEntryKey, ...]:
        return tuple(
            entry.key
            for item in self.tree.selectedItems()
            if isinstance((entry := item.data(0, Qt.ItemDataRole.UserRole)), CompletedEntry)
        )

    def _activate(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(entry, CompletedEntry):
            self.openRequested.emit(entry.key)

    def _select(self) -> None:
        if self._rendering:
            return
        keys = self.selected_keys()
        self.selected.emit(keys[0] if keys else None)

    def _menu(self, position: QPoint) -> None:
        item = self.tree.itemAt(position)
        if item is not None and isinstance(
            (entry := item.data(0, Qt.ItemDataRole.UserRole)), CompletedEntry
        ):
            self.tree.setCurrentItem(item)
            self.menuRequested.emit(entry.key, self.tree.viewport().mapToGlobal(position))

    def _next_page(self, key: Month, cursor: CompletionCursor | None) -> None:
        if cursor is None:
            return
        self._previous.setdefault(key, []).append(self._cursors.get(key))
        self._previous[key] = self._previous[key][-20:]
        self._cursors[key] = cursor
        self._page_number[key] = self._page_number.get(key, 0) + 1
        self.refresh()

    def _previous_page(self, key: Month) -> None:
        if self._previous.get(key):
            self._cursors[key] = self._previous[key].pop()
            self._page_number[key] = max(0, self._page_number.get(key, 0) - 1)
            self.refresh()

    def _first_page(self, key: Month) -> None:
        self._cursors.pop(key, None)
        self._previous.pop(key, None)
        self._page_number.pop(key, None)
        self.refresh()

    def _older(self) -> None:
        if self.result and self.result.has_older:
            self._before = self.result.oldest_start
            self._reset()
            self._collapsed_all = False
            self.result = None
            self.refresh()

    def _latest(self) -> None:
        self._before = None
        self._reset()
        self._collapsed_all = False
        self.result = None
        self.refresh()

    def suspend(self) -> None:
        self._jobs.invalidate()
        self._saved_selection = self.selected_keys()
        self._saved_scroll = self.tree.verticalScrollBar().value()
        self._rendering = True
        self.tree.clear()
        self._rendering = False
        self.result = None

    def shutdown(self) -> None:
        self._jobs.shutdown()
