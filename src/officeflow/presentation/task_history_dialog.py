"""Modeless, bounded history UI. Details never consume the timeline's height."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, Qt, QThread, Signal, Slot
from PySide6.QtGui import QCloseEvent, QColor, QPalette, QResizeEvent, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.task_comparison import TextComparison, compare_text
from officeflow.application.task_history import HistoryQuery, TaskHistoryService
from officeflow.domain.task_history import (
    FIELD_LABELS,
    FIELDS,
    HistoryEntry,
    HistoryPage,
    HistorySummary,
    scope_key,
)
from officeflow.presentation.background import OperationWorker, finish_thread
from officeflow.presentation.compact_labels import CompactLabel
from officeflow.presentation.dates import date_with_weekday, format_datetime
from officeflow.presentation.file_list import checkable_file_list
from officeflow.presentation.theme import LIGHT_STYLESHEET


def render_values(values: dict[str, Any] | None, timezone: str) -> str:
    if values is None:
        return "변경 전 기록 없음"
    lines: list[str] = []
    for field in FIELDS:
        lines.append("【" + FIELD_LABELS[field] + "】")
        value = values[field]
        if isinstance(value, dict):
            labels = {
                "value": "상태",
                "completed_at": "완료",
                "all_day": "종일",
                "starts_at": "시작",
                "ends_at": "종료",
                "timezone": "시간대",
                "recurrence_rule": "반복",
                "recurrence_until": "반복 종료",
                "occurrence_start": "원래 시작",
                "occurrence_end": "원래 종료",
                "effective_start": "변경된 시작",
                "effective_end": "변경된 종료",
            }
            for key, item in value.items():
                if isinstance(item, str) and key.endswith(("_at", "_start", "_end", "_until")):
                    item = format_datetime(datetime.fromisoformat(item), timezone)
                if key == "value":
                    item = {
                        "active": "진행",
                        "pending": "대기",
                        "completed": "완료",
                        "canceled": "취소",
                        "archived": "보관",
                        "skipped": "건너뛰기",
                    }.get(item, item)
                elif key == "all_day":
                    item = "예" if item else "아니요"
                lines.append(f"{labels.get(key, key)}: {'없음' if item is None else item}")
        else:
            if field == "priority":
                value = {
                    "normal": "보통",
                    "attention": "관심",
                    "important": "중요",
                    "urgent": "긴급",
                }.get(value, value)
            lines.append(str(value))
        lines.append("")
    return "\n".join(lines)


def render_saved_info(info: dict[str, Any] | None) -> str:
    if info is None:
        return "\n※ 이 변경 이력에는 체크리스트·파일을 보관하지 않았습니다."
    lines = ["", "【공통 체크리스트】"]
    lines.extend(
        ("☑ " if row["is_done"] else "☐ ") + row["content"] for row in info.get("checklist", [])
    )
    if not info.get("checklist"):
        lines.append("없음")
    lines.append("\n【보관 파일 · 내부 문서 내용 비교 아님】")
    for row in info.get("files", []):
        role = {"original": "원본", "work": "작업본", "general": "일반"}.get(
            row["role"], row["role"]
        )
        lines.append(
            f"[{role}] {row['name']} · {row['size']} bytes\n검증값: {row['checksum']}\n메모: {row['memo'] or '없음'}"
        )
    if not info.get("files"):
        lines.append("없음")
    return "\n".join(lines)


class _ReadDialog(QDialog):
    """At most one worker. Reject waits responsively before destroying Qt owners."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setModal(False)
        palette = self.palette()
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#F4F6FA"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#FFFFFF"))
        for role in (
            QPalette.ColorRole.Text,
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.ButtonText,
        ):
            palette.setColor(role, QColor("#172033"))
        self.setPalette(palette)
        self.setStyleSheet(
            LIGHT_STYLESHEET
            + """
            QAbstractSpinBox QLineEdit {
                padding: 0; border: none; border-radius: 0; background: transparent;
            }
            QCheckBox::indicator { width: 14px; height: 14px; background: #FFFFFF; border: 1px solid #879BB9; border-radius: 3px; }
            QCheckBox::indicator:checked { background: #2F6FED; border-color: #2F6FED; }
        """
        )
        self._thread: QThread | None = None
        self._worker: OperationWorker | None = None
        self._closed = False
        self._generation = 0
        self._running_token = 0
        self._success: Callable[[Any], None] = lambda _value: None
        self._pending: tuple[Callable[[], Any], Callable[[Any], None]] | None = None
        self.feedback = QLabel()
        self.feedback.setWordWrap(True)

    def run(self, operation: Callable[[], Any], success: Callable[[Any], None]) -> None:
        if self._closed:
            return
        self._generation += 1
        if self._thread is not None:
            self._pending = (operation, success)
            return
        token = self._generation
        self._running_token = token
        thread = QThread(self)
        worker = OperationWorker(lambda _cancel: (token, operation()))
        worker.moveToThread(thread)
        self._thread, self._worker, self._success = thread, worker, success
        worker.succeeded.connect(self._ready, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        thread.started.connect(worker.run)
        thread.start()

    @Slot(object)
    def _ready(self, value: Any) -> None:
        token, result = value
        if not self._closed and token == self._generation:
            try:
                self._success(result)
            except Exception as error:
                self.feedback.setText(str(error))

    @Slot(object)
    def _failed(self, error: object) -> None:
        if not self._closed and self._running_token == self._generation:
            self.feedback.setText(str(error))

    @Slot()
    def _finished(self) -> None:
        thread = self._thread
        if thread is not None:
            thread.wait()
            thread.deleteLater()
        self._thread, self._worker = None, None
        if self._pending is not None and not self._closed:
            operation, success = self._pending
            self._pending = None
            self.run(operation, success)

    def shutdown(self) -> None:
        self._closed = True
        self._pending = None
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None:
            finish_thread(self._thread, parent=self)

    def reject(self) -> None:
        self.shutdown()
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)


class HistoryCompareDialog(_ReadDialog):
    def __init__(
        self,
        title: str,
        left: dict[str, Any] | None,
        right: dict[str, Any],
        *,
        timezone: str,
        left_info: dict[str, Any] | None = None,
        right_info: dict[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(860, 620)
        self.setMinimumSize(400, 350)
        root = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setWordWrap(True)
        heading.setMaximumHeight(60)
        heading.setToolTip(title)
        root.addWidget(heading)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.before = QPlainTextEdit()
        self.after = QPlainTextEdit()
        for text, label in (
            (self.before, "이전 / 첫 번째 시점"),
            (self.after, "이후 / 두 번째 시점"),
        ):
            box = QWidget()
            layout = QVBoxLayout(box)
            layout.addWidget(QLabel(label))
            text.setReadOnly(True)
            text.setStyleSheet(
                "QPlainTextEdit, QPlainTextEdit QWidget {color:#172033; background:#FFFFFF;}"
            )
            layout.addWidget(text, 1)
            copy = QPushButton("텍스트 복사")
            copy.clicked.connect(lambda _checked=False, widget=text: self._copy(widget))
            layout.addWidget(copy)
            self.splitter.addWidget(box)
        root.addWidget(self.splitter, 1)
        root.addWidget(self.feedback)
        close = QPushButton("닫기")
        close.clicked.connect(self.reject)
        root.addWidget(close)
        self.run(lambda: self._prepare(left, right, timezone, left_info, right_info), self._render)

    @staticmethod
    def _copy(widget: QPlainTextEdit) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(widget.toPlainText())

    @staticmethod
    def _prepare(
        left: dict[str, Any] | None,
        right: dict[str, Any],
        timezone: str,
        left_info: dict[str, Any] | None = None,
        right_info: dict[str, Any] | None = None,
    ) -> tuple[str, str, str, TextComparison]:
        before, after = render_values(left, timezone), render_values(right, timezone)
        if left_info is not None or right_info is not None:
            before += render_saved_info(left_info)
            after += render_saved_info(right_info)
        changed = [FIELD_LABELS[key] for key in FIELDS if left is None or left[key] != right[key]]
        # The complete saved values are always shown. No semantic/units inference.
        note = "변경: " + ("·".join(changed) or "없음")
        if left_info is not None or right_info is not None:
            note = "업무 정보 " + note
            if left_info is None or right_info is None:
                note += " · 한 시점에는 체크리스트·파일 보관 정보 없음"
            else:
                retained_changes = [
                    label
                    for key, label in (("checklist", "체크리스트"), ("files", "파일 정보"))
                    if left_info.get(key, []) != right_info.get(key, [])
                ]
                note += " · 보관 정보 변경: " + ("·".join(retained_changes) or "없음")
        diff = compare_text(before, after)
        if diff.fallback:
            note += " · 비교 예산 초과: 변경 강조 생략 (전체 내용 유지)"
        return before, after, note, diff

    def _render(self, result: tuple[str, str, str, TextComparison]) -> None:
        self.before.setPlainText(result[0])
        self.after.setPlainText(result[1])
        self.feedback.setText(result[2])
        for widget, ranges, color in (
            (self.before, result[3].left, "#FDE2E2"),
            (self.after, result[3].right, "#D6F5E1"),
        ):
            if not ranges:
                widget.setExtraSelections([])
                continue
            text = widget.toPlainText()
            # Qt cursors count UTF-16 code units, not Python Unicode codepoints.
            offsets = [0]
            for char in text:
                offsets.append(offsets[-1] + (2 if ord(char) > 0xFFFF else 1))
            selections = []
            for start, end in ranges:
                selection = QTextEdit.ExtraSelection()
                selection.cursor = QTextCursor(widget.document())
                selection.cursor.setPosition(offsets[start])
                selection.cursor.setPosition(
                    offsets[min(end, len(text))], QTextCursor.MoveMode.KeepAnchor
                )
                selection.format = QTextCharFormat()
                selection.format.setBackground(QColor(color))
                selection.format.setForeground(QColor("#172033"))
                selections.append(selection)
            widget.setExtraSelections(selections)

    def resizeEvent(self, event: QResizeEvent) -> None:
        if hasattr(self, "splitter"):
            self.splitter.setOrientation(
                Qt.Orientation.Vertical if event.size().width() < 700 else Qt.Orientation.Horizontal
            )
        super().resizeEvent(event)


class TaskHistoryDialog(_ReadDialog):
    taskChanged = Signal(int)

    def __init__(
        self,
        task_id: int,
        title: str,
        service: TaskHistoryService,
        *,
        occurrence_start: datetime | None = None,
        timezone: str = "Asia/Seoul",
        read_only: bool = False,
        lifecycle: Any = None,
        export_service: Any = None,
        draft_guard: Callable[[], bool] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.task_id, self.service, self.timezone = task_id, service, timezone
        self.occurrence_start, self.read_only = occurrence_start, read_only
        self.lifecycle, self.export_service, self.draft_guard = (
            lifecycle,
            export_service,
            draft_guard,
        )
        self._cursors: list[tuple[datetime, int, int] | None] = [None]
        self._page = HistoryPage((), False)
        self._compared: list[tuple[str, int]] = []
        self._compare_rows: dict[tuple[str, int], HistorySummary] = {}
        self._visible_rows: dict[tuple[str, int], HistorySummary] = {}
        self._compare_mode = False
        self._rendering_compare = False
        self._children: list[Any] = []
        self.setWindowTitle("업무 이력 · " + title)
        self.resize(720, 520)
        self.setMinimumSize(420, 350)
        root = QVBoxLayout(self)
        header = QHBoxLayout()
        self.heading = CompactLabel(title)
        self.heading.setObjectName("pageTitle")
        self.heading.setMaximumHeight(60)
        self.heading.setToolTip(title)
        header.addWidget(self.heading, 1)
        if lifecycle is not None:
            capture = QPushButton("시점 보관…")
            capture.setEnabled(not read_only)
            capture.clicked.connect(self.open_capture)
            header.addWidget(capture)
            more = QPushButton("⋯")
            more.setAccessibleName("이력 추가 행동")
            menu = QMenu(more)
            menu.setStyleSheet(LIGHT_STYLESHEET)
            pending = menu.addAction("중단된 보관 확인…")
            pending.triggered.connect(self.open_pending)
            more.setMenu(menu)
            header.addWidget(more)
        root.addLayout(header)
        filters = QHBoxLayout()
        self.scope = QComboBox()
        self.scope.addItem("업무 공통", ("task",))
        if occurrence_start is not None:
            self.scope.addItem("업무 공통 + 이 회차", ("task", scope_key(occurrence_start)))
            self.scope.addItem("이 회차", (scope_key(occurrence_start),))
            self.scope.setCurrentIndex(1)
        self.scope.addItem("모든 회차 포함", ())
        self.kind = QComboBox()
        for label, kind in (
            ("종류 전체", None),
            ("변경", "change"),
            ("생성", "created"),
            ("시작 기준", "baseline"),
            ("시점 보관", "snapshot"),
        ):
            self.kind.addItem(label, kind)
        self.field = QComboBox()
        self.field.addItem("항목 전체", None)
        for key in FIELDS:
            self.field.addItem(FIELD_LABELS[key], key)
        for combo in (self.scope, self.kind, self.field):
            filters.addWidget(combo)
            combo.currentIndexChanged.connect(self.reset_page)
        root.addLayout(filters)
        dates = QHBoxLayout()
        self.date_filter = QCheckBox("기간")
        self.start_date = date_with_weekday(QDateEdit(QDate.currentDate().addMonths(-1)))
        self.end_date = date_with_weekday(QDateEdit(QDate.currentDate()))
        for date_edit in (self.start_date, self.end_date):
            date_edit.setMinimumHeight(36)
            date_edit.setCalendarPopup(True)
            date_edit.dateChanged.connect(self.reset_page)
            date_edit.setEnabled(False)
        self.date_filter.toggled.connect(self.start_date.setEnabled)
        self.date_filter.toggled.connect(self.end_date.setEnabled)
        self.date_filter.toggled.connect(self.reset_page)
        root.addWidget(self.date_filter)
        for widget in (self.start_date, self.end_date):
            dates.addWidget(widget)
        root.addLayout(dates)
        self.start_date.setVisible(False)
        self.end_date.setVisible(False)
        self.date_filter.toggled.connect(self.start_date.setVisible)
        self.date_filter.toggled.connect(self.end_date.setVisible)
        self.list = QListWidget()
        checkable_file_list(self.list)
        self.list.setObjectName("taskHistoryList")
        # Comparison/date controls must never squeeze the actual choices out of view.
        self.list.setMinimumHeight(100)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.itemActivated.connect(lambda _item: self.open_detail())
        self.list.itemChanged.connect(self._compare_checked)
        self.list.itemSelectionChanged.connect(self._update_normal_actions)
        root.addWidget(self.list, 1)
        self.compare_panel = QWidget(self)
        comparing = QVBoxLayout(self.compare_panel)
        comparing.setContentsMargins(0, 0, 0, 0)
        self.compare_hint = CompactLabel()
        comparing.addWidget(self.compare_hint)
        self.compare_labels: list[CompactLabel] = []
        self.compare_remove: list[QPushButton] = []
        for index in range(2):
            row = QHBoxLayout()
            choice_label = CompactLabel()
            button = QPushButton("해제")
            button.clicked.connect(lambda _checked=False, number=index: self.remove_compare(number))
            row.addWidget(choice_label, 1)
            row.addWidget(button)
            comparing.addLayout(row)
            self.compare_labels.append(choice_label)
            self.compare_remove.append(button)
        self.compare = QPushButton("비교하기")
        self.compare.clicked.connect(self.open_compare)
        cancel = QPushButton("취소")
        cancel.clicked.connect(self.reset_compare)
        compare_actions = QHBoxLayout()
        compare_actions.addStretch()
        compare_actions.addWidget(self.compare)
        compare_actions.addWidget(cancel)
        comparing.addLayout(compare_actions)
        self.compare_panel.hide()
        root.addWidget(self.compare_panel)
        root.addWidget(self.feedback)
        self.navigation = QWidget()
        navigation = QHBoxLayout(self.navigation)
        navigation.setContentsMargins(0, 0, 0, 0)
        self.previous = QPushButton("이전 페이지")
        self.next = QPushButton("다음 페이지")
        self.page_label = QLabel()
        for navigation_widget in (self.previous, self.page_label, self.next):
            navigation.addWidget(navigation_widget)
        self.previous.clicked.connect(self.previous_page)
        self.next.clicked.connect(self.next_page)
        root.addWidget(self.navigation)
        self.normal_actions = QWidget(self)
        buttons = QHBoxLayout(self.normal_actions)
        buttons.setContentsMargins(0, 0, 0, 0)
        self.detail = QPushButton("상세")
        self.detail.clicked.connect(self.open_detail)
        self.compare_menu_button = QPushButton("비교 ▾")
        comparison_menu = QMenu(self.compare_menu_button)
        comparison_menu.setStyleSheet(LIGHT_STYLESHEET)
        current = comparison_menu.addAction("선택 이력을 현재와 비교")
        current.setToolTip("선택한 과거 시점의 업무 정보 6항목을 지금 저장된 상태와 비교")
        current.triggered.connect(self.compare_current)
        self.current_compare_action = current
        choose = comparison_menu.addAction("두 시점 선택해서 비교…")
        choose.triggered.connect(self.begin_compare)
        self.compare_menu_button.setMenu(comparison_menu)
        self.refresh_button = QPushButton("새로고침")
        self.refresh_button.clicked.connect(self.reset_page)
        buttons.addStretch()
        for button in (self.detail, self.compare_menu_button, self.refresh_button):
            buttons.addWidget(button)
        root.addWidget(self.normal_actions)
        self.date_filter.toggled.connect(self._ensure_layout_room)
        self.taskChanged.connect(self._changed, Qt.ConnectionType.QueuedConnection)
        self._unsubscribe = service.subscribe(self.taskChanged.emit)
        self.finished.connect(lambda _code: self._unsubscribe())
        self.destroyed.connect(self._unsubscribe)
        self.load_page()

    def _ensure_layout_room(self) -> None:
        layout = self.layout()
        if layout is None:
            return
        layout.invalidate()
        layout.activate()
        self.setMinimumHeight(max(350, layout.minimumSize().height()))

    def query(self) -> HistoryQuery:
        zone = ZoneInfo(self.timezone)
        after = before = None
        if self.date_filter.isChecked():
            after = datetime.combine(
                cast(date, self.start_date.date().toPython()), time.min, zone
            ).astimezone(UTC)
            before = datetime.combine(
                cast(date, self.end_date.date().toPython()) + timedelta(days=1), time.min, zone
            ).astimezone(UTC)
        return HistoryQuery(
            tuple(self.scope.currentData()),
            self.kind.currentData(),
            self.field.currentData(),
            after,
            before,
            self._cursors[-1],
        )

    def reset_page(self) -> None:
        self._cursors = [None]
        self.load_page()

    def load_page(self) -> None:
        try:
            query = self.query()
        except ValueError as error:
            self.feedback.setText(str(error))
            return
        self.run(lambda: self.service.page(self.task_id, query), self._render_page)

    def _render_page(self, page: HistoryPage) -> None:
        if not page.items and len(self._cursors) > 1:
            self.previous_page()
            return
        self._page = page
        self.list.clear()
        self._visible_rows = {(row.kind, row.id): row for row in page.items}
        labels = {"created": "생성", "baseline": "기준", "change": "변경", "snapshot": "시점 보관"}
        for row in page.items:
            scope = (
                "업무 공통"
                if row.scope_key == "task"
                else "회차 "
                + format_datetime(cast(datetime, row.occurrence_start), self.timezone, short=True)
            )
            text = f"{format_datetime(row.recorded_at, self.timezone, short=True)}  [{labels[row.event_type]}] {row.summary} · {scope}"
            if row.kind == "snapshot":
                text += f" · 파일 {row.file_count}개"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, (row.kind, row.id))
            item.setToolTip(text)
            self.list.addItem(item)
        self.feedback.setText(
            f"이 페이지 {len(page.items)}건"
            if page.items
            else "아직 기록된 변경·보관이 없습니다. 기존 과거 변경은 소급 기록되지 않습니다."
        )
        self.navigation.setVisible(len(self._cursors) > 1 or page.has_next)
        self.previous.setEnabled(len(self._cursors) > 1)
        self.next.setEnabled(page.has_next)
        self.page_label.setText(f"{len(self._cursors)} 페이지")
        self._render_compare()
        self._update_normal_actions()

    def next_page(self) -> None:
        if self._page.has_next and self._page.items:
            last = self._page.items[-1]
            self._cursors.append((last.recorded_at, 1 if last.kind == "snapshot" else 0, last.id))
            self.load_page()

    def previous_page(self) -> None:
        if len(self._cursors) > 1:
            self._cursors.pop()
            self.load_page()

    def selected_id(self) -> tuple[str, int] | None:
        item = self.list.currentItem()
        return tuple(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def open_detail(self) -> None:
        identity = self.selected_id()
        if identity is not None:
            self.run(
                lambda: self.service.get(self.task_id, identity[1], identity[0]), self._detail_ready
            )

    def _detail_ready(self, entry: HistoryEntry) -> None:
        if entry.summary.kind == "snapshot" and self.lifecycle:
            from officeflow.presentation.task_snapshot_dialog import SnapshotDetailDialog

            dialog = SnapshotDetailDialog(
                entry, self.lifecycle, self.export_service, read_only=self.read_only, parent=self
            )
            self._keep(dialog)
            dialog.finished.connect(lambda _code: self.load_page())
            return
        self._show_compare(entry.summary.summary, entry.before, entry.after)

    def open_capture(self) -> None:
        if self.read_only or self.lifecycle is None:
            return
        if self.draft_guard is not None and not self.draft_guard():
            return
        from officeflow.presentation.task_snapshot_dialog import TaskSnapshotDialog

        dialog = TaskSnapshotDialog(
            self.task_id,
            self.lifecycle.snapshots,
            occurrence_start=self.occurrence_start,
            parent=self,
        )
        self._keep(dialog)

    def _keep(self, dialog: _ReadDialog) -> None:
        self._children.append(dialog)

        def closed(_code: int) -> None:
            dialog.shutdown()
            if dialog in self._children:
                self._children.remove(dialog)
            dialog.deleteLater()

        dialog.finished.connect(closed)
        dialog.show()

    def open_pending(self) -> None:
        if self.lifecycle is not None:
            from officeflow.presentation.task_snapshot_dialog import PendingSnapshotDialog

            self._keep(PendingSnapshotDialog(self.task_id, self.lifecycle.snapshots, parent=self))

    def _show_compare(
        self,
        title: str,
        before: dict[str, Any] | None,
        after: dict[str, Any],
        left_info: dict[str, Any] | None = None,
        right_info: dict[str, Any] | None = None,
    ) -> None:
        dialog = HistoryCompareDialog(
            title,
            before,
            after,
            timezone=self.timezone,
            left_info=left_info,
            right_info=right_info,
            parent=self,
        )
        self._keep(dialog)

    def choose_compare(self) -> None:
        identity = self.selected_id()
        if identity is None or identity in self._compared:
            return
        self._compare_mode = True
        self._select_compare(identity)

    def begin_compare(self) -> None:
        self._compare_mode = True
        self.choose_compare()
        self._render_compare()

    def _select_compare(self, identity: tuple[str, int]) -> None:
        row = self._visible_rows.get(identity)
        if row is None or identity in self._compared:
            return
        if len(self._compared) == 2:
            self.feedback.setText(
                "최대 두 시점을 고를 수 있습니다. 기존 시점을 해제한 뒤 고르세요."
            )
            self._render_compare()
            return
        if self._compared and self._compare_rows[self._compared[0]].scope_key != row.scope_key:
            self.feedback.setText("업무 공통끼리 또는 같은 회차끼리 선택하세요.")
            self._render_compare()
            return
        self._compared.append(identity)
        self._compare_rows[identity] = row
        self._render_compare()

    def _compare_checked(self, item: QListWidgetItem) -> None:
        if self._rendering_compare or not self._compare_mode:
            return
        identity = tuple(item.data(Qt.ItemDataRole.UserRole))
        if item.checkState() == Qt.CheckState.Checked:
            self._select_compare(identity)
        elif identity in self._compared:
            self.remove_compare(self._compared.index(identity))

    def remove_compare(self, index: int) -> None:
        if 0 <= index < len(self._compared):
            self._compare_rows.pop(self._compared.pop(index), None)
        self._render_compare()

    def _render_compare(self) -> None:
        self._rendering_compare = True
        for index in range(self.list.count()):
            item = self.list.item(index)
            key = tuple(item.data(Qt.ItemDataRole.UserRole))
            if self._compare_mode:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if key in self._compared else Qt.CheckState.Unchecked
                )
            else:
                item.setData(Qt.ItemDataRole.CheckStateRole, None)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        self._rendering_compare = False
        self.compare_panel.setVisible(self._compare_mode)
        self.normal_actions.setVisible(not self._compare_mode)
        self.compare_hint.setText(f"비교할 두 시점을 체크하세요. {len(self._compared)}/2")
        for index, label in enumerate(self.compare_labels):
            selected = index < len(self._compared)
            self.compare_remove[index].setVisible(selected)
            if selected:
                key = self._compared[index]
                row = self._compare_rows[key]
                scope = (
                    "업무 공통"
                    if row.scope_key == "task"
                    else "회차 "
                    + format_datetime(
                        cast(datetime, row.occurrence_start), self.timezone, short=True
                    )
                )
                label.setText(
                    f"{index + 1}. {format_datetime(row.recorded_at, self.timezone, short=True)} · {row.summary} · {scope}"
                    + (" (현재 화면 밖)" if key not in self._visible_rows else "")
                )
            else:
                label.setText(f"{index + 1}. 아직 선택하지 않음")
        self.compare.setEnabled(len(self._compared) == 2)
        self._ensure_layout_room()

    def _update_normal_actions(self) -> None:
        if hasattr(self, "detail"):
            self.detail.setEnabled(self.selected_id() is not None)
            self.current_compare_action.setEnabled(self.selected_id() is not None)

    def reset_compare(self) -> None:
        self._compared.clear()
        self._compare_rows.clear()
        self._compare_mode = False
        self._render_compare()

    def open_compare(self) -> None:
        if len(self._compared) != 2:
            self.feedback.setText("비교할 두 시점을 선택하세요.")
            return
        left, right = sorted(
            self._compared, key=lambda key: (self._compare_rows[key].recorded_at, key)
        )
        self.run(
            lambda: self.service.compare(self.task_id, left, right),
            lambda entries: self._show_compare(
                "두 시점 비교 · 과거 → 최근",
                entries[0].after,
                entries[1].after,
                entries[0].saved_info,
                entries[1].saved_info,
            ),
        )

    def compare_current(self) -> None:
        identity = self.selected_id()
        if identity is None:
            self.feedback.setText("현재 상태와 비교할 시점을 선택하세요.")
            return

        self.run(
            lambda: self.service.compare_current(self.task_id, identity),
            lambda value: self._show_compare(
                "현재 상태와 비교 (업무 정보 6항목) · 조회 "
                + format_datetime(value[2], self.timezone),
                value[0].after,
                value[1],
            ),
        )

    @Slot(int)
    def _changed(self, task_id: int) -> None:
        if task_id == self.task_id and not self._closed:
            if len(self._cursors) == 1:
                self.load_page()
            else:
                self.feedback.setText("새 이력 있음 · 새로고침으로 확인하세요.")

    def shutdown(self) -> None:
        self._unsubscribe()
        for dialog in tuple(self._children):
            dialog.shutdown()
            dialog.close()
        super().shutdown()
