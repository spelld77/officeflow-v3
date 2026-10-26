from __future__ import annotations

from datetime import datetime
from typing import cast

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidgetAction,
)

from officeflow.application.recent_tasks import RecentTaskRef, RecentTasks, RecentTaskSummary
from officeflow.infrastructure.recent_tasks import RecentTaskStore
from officeflow.presentation.dates import format_datetime
from officeflow.presentation.latest_job import LatestJob
from officeflow.presentation.theme import LIGHT_STYLESHEET

Resolved = tuple[tuple[RecentTaskSummary, RecentTaskRef], ...]


class RecentTaskController(QObject):
    openRequested = Signal(object)
    pinnedRequested = Signal()
    warning = Signal(str)

    def __init__(
        self,
        service: RecentTasks,
        button: QPushButton,
        *,
        timezone: str,
        store: RecentTaskStore | None = None,
    ) -> None:
        super().__init__(button)
        self.service, self.store, self.button, self.timezone = service, store, button, timezone
        self.menu = QMenu(button)
        self.menu.setStyleSheet(LIGHT_STYLESHEET)
        self.menu.setToolTipsVisible(True)
        self.more: QDialog | None = None
        self._listing: QListWidget | None = None
        self._resolved: Resolved = ()
        self._pinned: tuple[RecentTaskSummary, ...] = ()
        self._reader, self._writer = LatestJob(self), LatestJob(self)
        self._reader.ready.connect(self._ready)
        self._reader.failed.connect(
            lambda _error: self.warning.emit("최근 업무를 불러오지 못했습니다. 다시 시도해 주세요.")
        )
        self._writer.failed.connect(self._write_failed)
        self._warned = False
        self._changed = False
        self._loaded = store is None
        self._cleared = False
        self._queried_refs: tuple[RecentTaskRef, ...] = ()
        self._mode = "load"
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._save)
        button.clicked.connect(self.show_menu)
        if store:
            button.setEnabled(False)
            self._reader.submit(store.load)

    def _write_failed(self, _error: str) -> None:
        if not self._warned:
            self._warned = True
            self.warning.emit(
                "최근 기록을 저장하지 못했습니다. 이번 실행에서는 계속 사용할 수 있습니다."
            )

    def record(
        self, task_id: int, created_at: datetime, occurrence: datetime | None, now: datetime
    ) -> None:
        self.service.record_viewed(RecentTaskRef(task_id, created_at, occurrence, now))
        self._changed = True
        if self.store:
            self.store.reserve()
        self._timer.start()

    def _save(self) -> None:
        if self.store:
            store, refs = self.store, self.service.entries
            revision = store.reserve()
            self._writer.submit(lambda: store.save(refs, revision))

    def clear(self) -> None:
        self._changed = True
        self._cleared = True
        self._loaded = True
        self._reader.invalidate()
        self.button.setEnabled(True)
        self.service.entries = ()
        self._resolved = ()
        if self.store:
            self.store.reserve()
        self._timer.start()
        if self._listing:
            self._listing.clear()
        self.menu.close()

    def show_menu(self) -> None:
        self._mode = "menu"
        refs = self.service.entries
        self._queried_refs = refs
        self.menu.clear()
        self.menu.addAction("불러오는 중…").setEnabled(False)
        self.menu.popup(self.button.mapToGlobal(QPoint(0, self.button.height())))
        self._reader.submit(lambda: (self.service.resolve(refs), self.service.repository.pinned()))

    def _ready(self, value: object) -> None:
        if self._mode == "load":
            self._loaded = True
            stored = cast(tuple[RecentTaskRef, ...], value)
            if not self._changed:
                self.service.entries = stored
            elif not self._cleared:
                seen = {ref.task_id for ref in self.service.entries}
                self.service.entries = (
                    *self.service.entries,
                    *(ref for ref in stored if ref.task_id not in seen),
                )[:20]
                self._timer.start()
            self.button.setEnabled(True)
            return
        self._resolved, self._pinned = cast(tuple[Resolved, tuple[RecentTaskSummary, ...]], value)
        valid = {ref for _summary, ref in self._resolved}
        remaining = tuple(
            ref for ref in self.service.entries if ref not in self._queried_refs or ref in valid
        )
        if remaining != self.service.entries:
            self.service.entries = remaining
            self._timer.start()
        if self._mode == "more":
            self._fill_more()
        elif self.menu.isVisible():
            self._fill_menu()

    def _label(self, summary: RecentTaskSummary, ref: RecentTaskRef | None) -> str:
        status = {
            "active": "진행",
            "pending": "대기",
            "completed": "완료",
            "canceled": "취소",
            "archived": "보관",
        }.get(summary.status, summary.status)
        if ref:
            status += f" · 열람 {format_datetime(ref.viewed_at, self.timezone)}"
            if ref.occurrence_start:
                status += f" · 회차 {format_datetime(ref.occurrence_start, self.timezone)}"
        return status

    def _add(self, summary: RecentTaskSummary, ref: RecentTaskRef | None) -> None:
        action = QWidgetAction(self.menu)
        button = QPushButton(self.menu)
        button.setMinimumHeight(54)
        button.setText(
            self.button.fontMetrics().elidedText(summary.title, Qt.TextElideMode.ElideRight, 330)
            + "\n"
            + self.button.fontMetrics().elidedText(
                self._label(summary, ref), Qt.TextElideMode.ElideRight, 330
            )
        )
        button.setToolTip(summary.title + "\n" + self._label(summary, ref))
        button.setStyleSheet(
            "QPushButton { text-align: left; padding: 5px 10px; background: white; color: #172033; } QPushButton:hover { background: #e8f0ff; }"
        )

        def open_item() -> None:
            self.menu.close()
            self.openRequested.emit(ref or self.service.reference(summary))

        button.clicked.connect(open_item)
        action.triggered.connect(open_item)
        action.setDefaultWidget(button)
        self.menu.addAction(action)

    def _fill_menu(self) -> None:
        self.menu.clear()
        screen = self.button.screen().availableGeometry()
        row_budget = max(1, min(8, (screen.height() - 210) // 56))
        pinned = self._pinned[: min(3, row_budget)]
        if pinned:
            self._section("고정 업무")
            for summary in pinned:
                self._add(summary, None)
            self.menu.addAction("고정 전체 보기…", self.pinnedRequested.emit)
        seen = {r.task_id for r in pinned}
        recent = tuple((s, r) for s, r in self._resolved if s.task_id not in seen)[
            : min(5, max(0, row_budget - len(pinned)))
        ]
        self._section("최근 본 업무")
        for summary, ref in recent:
            self._add(summary, ref)
        if not recent:
            self.menu.addAction("최근 본 업무 없음").setEnabled(False)
        self.menu.addAction("최근 더 보기…", self.show_more)
        self.menu.adjustSize()
        position = self.button.mapToGlobal(QPoint(0, self.button.height()))
        position.setX(max(screen.left(), min(position.x(), screen.right() - self.menu.width())))
        if position.y() + self.menu.height() > screen.bottom():
            position.setY(
                max(screen.top(), self.button.mapToGlobal(QPoint(0, 0)).y() - self.menu.height())
            )
        self.menu.move(position)

    def _section(self, text: str) -> None:
        action = QWidgetAction(self.menu)
        label = QLabel(text, self.menu)
        label.setStyleSheet(
            "color: #52627A; background: #F3F6FA; font-weight: bold; padding: 6px 10px;"
        )
        action.setDefaultWidget(label)
        self.menu.addAction(action)

    def show_more(self) -> None:
        self._mode = "more"
        if self.more is None:
            dialog = QDialog(self.button.window())
            dialog.setWindowTitle("최근 본 업무")
            dialog.setStyleSheet(LIGHT_STYLESHEET)
            dialog.resize(490, 480)
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel("이 PC에서 최근 본 업무 · 최대 20개", dialog))
            self._listing = QListWidget(dialog)
            self._listing.setStyleSheet("""
                QListWidget, QListWidget QWidget#qt_scrollarea_viewport {
                    background: white; color: #172033;
                    selection-color: #172033; selection-background-color: #DFE9FF;
                }
                QListWidget::item { padding: 8px; }
                QListWidget::item:selected { background: #DFE9FF; color: #172033; }
                QScrollBar:vertical { background: #EDF1F7; width: 13px; }
                QScrollBar::handle:vertical { background: #7FA3EF; min-height: 28px; }
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
                QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
            """)
            self._listing.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
            self._listing.itemActivated.connect(
                lambda item: self.openRequested.emit(item.data(Qt.ItemDataRole.UserRole))
            )
            layout.addWidget(self._listing, 1)
            actions = QHBoxLayout()
            clear = QPushButton("최근 기록 비우기", dialog)
            clear.clicked.connect(self.clear)
            actions.addWidget(clear)
            close = QPushButton("닫기", dialog)
            close.clicked.connect(dialog.reject)
            actions.addWidget(close)
            layout.addLayout(actions)
            self.more = dialog

            def finished(_result: int) -> None:
                self.more, self._listing = None, None
                dialog.deleteLater()

            dialog.finished.connect(finished)
        self.more.show()
        self.more.raise_()
        refs = self.service.entries
        self._queried_refs = refs
        self._reader.submit(lambda: (self.service.resolve(refs), self.service.repository.pinned()))

    def _fill_more(self) -> None:
        if self._listing is None:
            return
        self._listing.clear()
        for summary, ref in self._resolved:
            item = QListWidgetItem(summary.title + "\n" + self._label(summary, ref), self._listing)
            item.setData(Qt.ItemDataRole.UserRole, ref)
            item.setToolTip(item.text())

    def shutdown(self) -> None:
        self._timer.stop()
        self.menu.close()
        if self.more:
            self.more.reject()
        self._reader.shutdown()
        store, refs = self.store, self.service.entries
        revision = store.reserve() if store else 0

        def final_save() -> None:
            if store is None or (not self._loaded and not self._changed):
                return
            try:
                store.save(refs, revision)
            except (OSError, ValueError):
                self._write_failed("save")

        self._writer.shutdown(final_save)
