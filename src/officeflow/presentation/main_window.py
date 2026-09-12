from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from itertools import pairwise
from typing import ClassVar

from PySide6.QtCore import QModelIndex, QPoint, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QKeySequence, QResizeEvent, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.tasks import TaskService, TaskView
from officeflow.domain.enums import TaskStatus
from officeflow.domain.task import Task, TaskValidationError
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.task_editor import TaskEditorDialog
from officeflow.presentation.task_list import TaskItemDelegate, TaskListModel, format_task_schedule
from officeflow.presentation.theme import LIGHT_STYLESHEET

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    DETAIL_BREAKPOINT = 1100
    COMPACT_BREAKPOINT = 850
    MINIMUM_WIDTH = 760
    MINIMUM_HEIGHT = 560

    VIEW_LABELS: ClassVar[dict[TaskView, tuple[str, str]]] = {
        TaskView.TODAY: ("오늘", "오늘과 겹치는 업무를 일정순으로 보여드립니다."),
        TaskView.UPCOMING: ("예정", "오늘 이후에 시작하는 업무입니다."),
        TaskView.IMPORTANT: ("중요", "중요 또는 긴급으로 지정한 업무입니다."),
        TaskView.PENDING: ("대기", "잠시 보류한 업무입니다."),
        TaskView.COMPLETED: ("완료", "완료한 업무 기록입니다."),
        TaskView.ALL: ("전체 업무", "일정이 없는 업무를 포함한 전체 목록입니다."),
    }

    def __init__(
        self,
        settings: AppSettings,
        task_service: TaskService,
        save_settings: Callable[[AppSettings], None] | None = None,
        on_shutdown: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._task_service = task_service
        self._save_settings = save_settings
        self._on_shutdown = on_shutdown
        self._shutdown_done = False
        self._current_view = TaskView.TODAY
        self._selected_task_id: int | None = None
        self._compact_navigation = False
        self._compact_summaries = False
        self._nav_buttons: list[QPushButton] = []
        self._view_buttons: dict[TaskView, QPushButton] = {}
        self._summary_frames: list[QFrame] = []
        self._summary_counts: dict[str, QLabel] = {}

        self.setWindowTitle("OfficeFlow v3")
        self.resize(settings.window_width, settings.window_height)
        self.setMinimumSize(self.MINIMUM_WIDTH, self.MINIMUM_HEIGHT)
        self.setStyleSheet(LIGHT_STYLESHEET)

        root = QWidget()
        root.setObjectName("appRoot")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._sidebar = self._build_sidebar()
        layout.addWidget(self._sidebar)
        layout.addWidget(self._build_workspace(), 1)
        self.setCentralWidget(root)
        self._configure_input_tab_order()

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._refresh_tasks)
        self._search.textChanged.connect(self._search_timer.start)

        self._new_task_shortcut = QShortcut(QKeySequence("Ctrl+N"), self)
        self._new_task_shortcut.activated.connect(self._open_new_task)
        self._search_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self._search_shortcut.activated.connect(self._search.setFocus)
        self._open_task_shortcut = QShortcut(QKeySequence("Return"), self)
        self._open_task_shortcut.activated.connect(self._open_selected_task)

        self._restore_window_position(settings)
        self._apply_responsive_layout()
        self._refresh_tasks()

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(212)
        self._sidebar_layout = QVBoxLayout(sidebar)
        self._sidebar_layout.setContentsMargins(18, 24, 18, 20)
        self._sidebar_layout.setSpacing(6)

        self._brand_title = self._named_label("OfficeFlow", "brandTitle")
        self._brand_caption = self._named_label("나의 업무 흐름", "brandCaption")
        self._sidebar_layout.addWidget(self._brand_title)
        self._sidebar_layout.addWidget(self._brand_caption)
        self._sidebar_layout.addSpacing(22)

        for index, (label, view) in enumerate(
            (
                ("오늘", TaskView.TODAY),
                ("예정", TaskView.UPCOMING),
                ("중요", TaskView.IMPORTANT),
                ("대기", TaskView.PENDING),
                ("완료", TaskView.COMPLETED),
                ("전체 업무", TaskView.ALL),
            )
        ):
            self._sidebar_layout.addWidget(
                self._create_nav_button(label, view=view, selected=index == 0)
            )

        self._sidebar_layout.addSpacing(16)
        for label in ("캘린더", "업무일지", "설정"):
            button = self._create_nav_button(label)
            button.setEnabled(False)
            button.setToolTip("후속 단계에서 연결됩니다.")
            self._sidebar_layout.addWidget(button)

        self._sidebar_layout.addStretch()
        self._version_label = self._named_label("v3.0 · Phase 3A", "brandCaption")
        self._sidebar_layout.addWidget(self._version_label)
        return sidebar

    def _build_workspace(self) -> QWidget:
        workspace = QWidget()
        self._workspace_layout = QVBoxLayout(workspace)
        self._workspace_layout.setContentsMargins(24, 20, 24, 24)
        self._workspace_layout.setSpacing(16)
        self._workspace_layout.addWidget(self._build_top_bar())

        self._body_layout = QHBoxLayout()
        self._body_layout.setSpacing(16)
        self._body_layout.addWidget(self._build_content(), 3)
        self._detail_panel = self._build_detail()
        self._body_layout.addWidget(self._detail_panel, 2)
        self._workspace_layout.addLayout(self._body_layout, 1)
        return workspace

    def _build_top_bar(self) -> QWidget:
        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        layout = QHBoxLayout(top_bar)
        layout.setContentsMargins(14, 12, 14, 12)

        self._search = QLineEdit()
        self._search.setObjectName("taskSearchEdit")
        self._search.setPlaceholderText("업무와 내용 검색  (Ctrl+K)")
        self._search.setClearButtonEnabled(True)
        self._search.setMaximumWidth(520)
        layout.addWidget(self._search, 1)
        layout.addStretch()

        self._add_button = QPushButton("+ 새 업무")
        self._add_button.setObjectName("primaryButton")
        self._add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_button.clicked.connect(self._open_new_task)
        layout.addWidget(self._add_button)
        return top_bar

    def _build_content(self) -> QWidget:
        card = QFrame()
        card.setObjectName("contentCard")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)

        heading = QHBoxLayout()
        titles = QVBoxLayout()
        self._page_title = self._named_label("오늘", "pageTitle")
        self._page_caption = self._named_label(self.VIEW_LABELS[TaskView.TODAY][1], "mutedText")
        self._page_caption.setWordWrap(True)
        titles.addWidget(self._page_title)
        titles.addWidget(self._page_caption)
        heading.addLayout(titles)
        heading.addStretch()
        self._open_selected_button = QPushButton("선택 업무 열기")
        self._open_selected_button.clicked.connect(self._open_selected_task)
        self._open_selected_button.hide()
        heading.addWidget(self._open_selected_button)
        filter_button = QPushButton("일정순")
        filter_button.setEnabled(False)
        filter_button.setToolTip("복합 정렬은 Phase 3에서 제공됩니다.")
        heading.addWidget(filter_button)
        layout.addLayout(heading)

        self._summary_layout = QGridLayout()
        self._summary_layout.setSpacing(10)
        for key, name in (
            ("overdue", "지연"),
            ("in_progress", "진행 중"),
            ("today", "오늘"),
            ("completed", "완료"),
        ):
            frame = QFrame()
            frame.setProperty("summary", True)
            item_layout = QVBoxLayout(frame)
            item_layout.setContentsMargins(14, 9, 14, 9)
            count = QLabel("0")
            count.setProperty("count", True)
            self._summary_counts[key] = count
            item_layout.addWidget(count)
            item_layout.addWidget(self._named_label(name, "mutedText"))
            self._summary_frames.append(frame)
        self._arrange_summary_cards(compact=False)
        layout.addLayout(self._summary_layout)

        quick_add = QHBoxLayout()
        self._quick_add_edit = QLineEdit()
        self._quick_add_edit.setObjectName("quickAddEdit")
        self._quick_add_edit.setPlaceholderText("빠른 등록: 제목 입력 후 Enter")
        self._quick_add_edit.returnPressed.connect(self._quick_add_task)
        quick_add.addWidget(self._quick_add_edit, 1)
        self._quick_add_button = QPushButton("추가")
        self._quick_add_button.setObjectName("quickAddButton")
        self._quick_add_button.clicked.connect(self._quick_add_task)
        quick_add.addWidget(self._quick_add_button)
        layout.addLayout(quick_add)

        self._task_model = TaskListModel()
        self._task_list = QListView()
        self._task_list.setObjectName("taskList")
        self._task_list.setModel(self._task_model)
        self._task_list.setItemDelegate(TaskItemDelegate(self._task_list))
        self._task_list.setMouseTracking(True)
        self._task_list.setFrameShape(QFrame.Shape.NoFrame)
        self._task_list.setSpacing(1)
        self._task_list.setUniformItemSizes(True)
        self._task_list.selectionModel().currentChanged.connect(self._on_selection_changed)
        self._task_list.doubleClicked.connect(self._open_task_from_index)
        layout.addWidget(self._task_list, 1)

        self._empty_panel = QFrame()
        empty_layout = QVBoxLayout(self._empty_panel)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(
            QLabel("표시할 업무가 없습니다."), alignment=Qt.AlignmentFlag.AlignCenter
        )
        empty_description = self._named_label(
            "빠르게 등록하거나 다른 보기를 선택해 보세요.", "mutedText"
        )
        empty_description.setWordWrap(True)
        empty_description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_description, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._empty_panel, 1)
        return card

    def _build_detail(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("detailPanel")
        panel.setMinimumWidth(290)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(10)

        self._detail_title = self._named_label("업무 상세", "pageTitle")
        self._detail_title.setWordWrap(True)
        self._detail_status = self._named_label("목록에서 업무를 선택하세요.", "mutedText")
        self._detail_schedule = self._named_label("", "mutedText")
        self._detail_schedule.setWordWrap(True)
        self._detail_description = QLabel("")
        self._detail_description.setWordWrap(True)
        self._detail_description.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self._detail_title)
        layout.addWidget(self._detail_status)
        layout.addWidget(self._detail_schedule)
        layout.addSpacing(8)
        layout.addWidget(self._detail_description)
        layout.addStretch()

        actions = QHBoxLayout()
        self._pending_button = QPushButton("대기")
        self._pending_button.clicked.connect(lambda: self._transition_selected(TaskStatus.PENDING))
        self._complete_button = QPushButton("완료")
        self._complete_button.clicked.connect(
            lambda: self._transition_selected(TaskStatus.COMPLETED)
        )
        self._archive_button = QPushButton("보관")
        self._archive_button.clicked.connect(lambda: self._transition_selected(TaskStatus.ARCHIVED))
        self._edit_button = QPushButton("수정")
        self._edit_button.setObjectName("primaryButton")
        self._edit_button.clicked.connect(self._open_selected_task)
        for button in (
            self._pending_button,
            self._complete_button,
            self._archive_button,
            self._edit_button,
        ):
            button.setEnabled(False)
            actions.addWidget(button)
        layout.addLayout(actions)
        return panel

    def _configure_input_tab_order(self) -> None:
        fields: tuple[QWidget, ...] = (
            self._search,
            self._add_button,
            self._quick_add_edit,
            self._quick_add_button,
            self._task_list,
        )
        for current, following in pairwise(fields):
            self.setTabOrder(current, following)

    def _create_nav_button(
        self,
        label: str,
        *,
        view: TaskView | None = None,
        selected: bool = False,
    ) -> QPushButton:
        button = QPushButton(label)
        button.setProperty("nav", True)
        button.setProperty("selected", selected)
        button.setProperty("fullLabel", label)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if view is not None:
            button.clicked.connect(
                lambda _checked=False, selected_view=view: self._set_view(selected_view)
            )
            self._view_buttons[view] = button
        self._nav_buttons.append(button)
        return button

    def _set_view(self, view: TaskView) -> None:
        self._current_view = view
        title, caption = self.VIEW_LABELS[view]
        self._page_title.setText(title)
        self._page_caption.setText(caption)
        for button_view, button in self._view_buttons.items():
            button.setProperty("selected", button_view is view)
            button.style().unpolish(button)
            button.style().polish(button)
        self._selected_task_id = None
        self._refresh_tasks()

    def _refresh_tasks(self) -> None:
        try:
            selected_id = self._selected_task_id
            tasks = self._task_service.list(
                self._current_view,
                search=self._search.text() if hasattr(self, "_search") else "",
            )
            self._task_model.set_tasks(tasks)
            self._task_list.setVisible(bool(tasks))
            self._empty_panel.setVisible(not tasks)

            summary = self._task_service.summary()
            self._summary_counts["overdue"].setText(str(summary.overdue))
            self._summary_counts["in_progress"].setText(str(summary.in_progress))
            self._summary_counts["today"].setText(str(summary.today))
            self._summary_counts["completed"].setText(str(summary.completed_today))

            if selected_id is not None:
                index = self._task_model.index_for_task(selected_id)
                if index.isValid():
                    self._task_list.setCurrentIndex(index)
                    self._update_detail(self._task_model.task_at(index))
                    return
            self._selected_task_id = None
            self._update_detail(None)
        except Exception as error:
            logger.exception("Failed to refresh tasks")
            self.statusBar().showMessage(f"업무를 불러오지 못했습니다: {error}", 5000)

    def _quick_add_task(self) -> None:
        title = self._quick_add_edit.text()
        try:
            task = self._task_service.quick_add(title, view=self._current_view)
        except TaskValidationError as error:
            self.statusBar().showMessage(str(error), 4000)
            self._quick_add_edit.setFocus()
            return
        except Exception as error:
            self._show_error("업무를 등록하지 못했습니다.", error)
            return

        self._quick_add_edit.clear()
        if self._current_view not in {TaskView.TODAY, TaskView.ALL}:
            self._set_view(TaskView.ALL)
        self._selected_task_id = task.id
        self._refresh_tasks()
        self.statusBar().showMessage("업무를 등록했습니다.", 2500)

    def _open_new_task(self) -> None:
        self._open_editor(None)

    def _open_selected_task(self) -> None:
        if self._selected_task_id is None:
            return
        try:
            self._open_editor(self._task_service.get(self._selected_task_id))
        except Exception as error:
            self._show_error("업무를 열지 못했습니다.", error)

    def _open_task_from_index(self, index: QModelIndex) -> None:
        task = self._task_model.task_at(index)
        if task is not None:
            self._selected_task_id = task.id
            self._open_editor(task)

    def _open_editor(self, task: Task | None) -> None:
        editor = TaskEditorDialog(timezone=self._settings.timezone, task=task, parent=self)
        editor.setStyleSheet(LIGHT_STYLESHEET)
        if editor.exec() != TaskEditorDialog.DialogCode.Accepted:
            return
        try:
            saved = (
                self._task_service.create(editor.draft())
                if task is None or task.id is None
                else self._task_service.update(task.id, editor.draft())
            )
        except (TaskValidationError, ValueError) as error:
            self._show_error("입력 내용을 확인하세요.", error)
            return
        except Exception as error:
            self._show_error("업무를 저장하지 못했습니다.", error)
            return
        self._selected_task_id = saved.id
        self._refresh_tasks()
        self.statusBar().showMessage("업무를 저장했습니다.", 2500)

    def _on_selection_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        task = self._task_model.task_at(current)
        self._selected_task_id = task.id if task else None
        self._update_detail(task)
        self._apply_responsive_layout()

    def _update_detail(self, task: Task | None) -> None:
        if task is None:
            self._detail_title.setText("업무 상세")
            self._detail_status.setText("목록에서 업무를 선택하세요.")
            self._detail_schedule.clear()
            self._detail_description.clear()
            for button in (
                self._pending_button,
                self._complete_button,
                self._archive_button,
                self._edit_button,
            ):
                button.setEnabled(False)
            self._open_selected_button.hide()
            return

        status_labels = {
            TaskStatus.ACTIVE: "진행",
            TaskStatus.PENDING: "대기",
            TaskStatus.COMPLETED: "완료",
            TaskStatus.CANCELED: "취소",
            TaskStatus.ARCHIVED: "보관",
        }
        priority_labels = {
            "normal": "보통",
            "attention": "관심",
            "important": "중요",
            "urgent": "긴급",
        }
        self._detail_title.setText(task.title)
        self._detail_status.setText(
            f"{status_labels[task.status]} · 중요도 {priority_labels[task.priority.value]}"
        )
        self._detail_schedule.setText(format_task_schedule(task))
        self._detail_description.setText(task.description or "설명이 없습니다.")
        self._edit_button.setEnabled(True)
        self._pending_button.setEnabled(task.status is TaskStatus.ACTIVE)
        self._complete_button.setEnabled(task.status in {TaskStatus.ACTIVE, TaskStatus.PENDING})
        self._archive_button.setEnabled(task.status is not TaskStatus.ARCHIVED)
        self._open_selected_button.setVisible(not self._detail_panel.isVisible())

    def _transition_selected(self, status: TaskStatus) -> None:
        if self._selected_task_id is None:
            return
        try:
            self._task_service.transition(self._selected_task_id, status)
        except (TaskValidationError, LookupError) as error:
            self._show_error("상태를 변경하지 못했습니다.", error)
            return
        self._refresh_tasks()
        self.statusBar().showMessage("업무 상태를 변경했습니다.", 2500)

    def _show_error(self, title: str, error: Exception) -> None:
        logger.exception(title, exc_info=error)
        QMessageBox.critical(self, title, str(error))

    def _apply_responsive_layout(self) -> None:
        width = self.width()
        compact_navigation = width < self.COMPACT_BREAKPOINT
        show_detail = width >= self.DETAIL_BREAKPOINT

        self._detail_panel.setVisible(show_detail)
        self._body_layout.setSpacing(16 if show_detail else 0)
        self._open_selected_button.setVisible(
            not show_detail and self._selected_task_id is not None
        )

        if compact_navigation:
            self._sidebar.setFixedWidth(88)
            self._sidebar_layout.setContentsMargins(10, 20, 10, 16)
            self._workspace_layout.setContentsMargins(12, 12, 12, 14)
            self._brand_title.setText("OF")
            self._brand_caption.hide()
            self._version_label.setText("v3")
            self._search.setPlaceholderText("업무 검색")
            self._add_button.setText("+ 업무")
        else:
            self._sidebar.setFixedWidth(212 if show_detail else 180)
            self._sidebar_layout.setContentsMargins(18, 24, 18, 20)
            margin = 24 if show_detail else 18
            self._workspace_layout.setContentsMargins(margin, 18, margin, 20)
            self._brand_title.setText("OfficeFlow")
            self._brand_caption.show()
            self._version_label.setText("v3.0 · Phase 3A")
            self._search.setPlaceholderText("업무와 내용 검색  (Ctrl+K)")
            self._add_button.setText("+ 새 업무")

        if compact_navigation != self._compact_navigation:
            self._compact_navigation = compact_navigation
            compact_labels = {
                "전체 업무": "전체",
                "캘린더": "달력",
                "업무일지": "일지",
            }
            for button in self._nav_buttons:
                full_label = str(button.property("fullLabel"))
                button.setText(
                    compact_labels.get(full_label, full_label) if compact_navigation else full_label
                )
                button.setProperty("compact", compact_navigation)
                button.style().unpolish(button)
                button.style().polish(button)

        if compact_navigation != self._compact_summaries:
            self._arrange_summary_cards(compact=compact_navigation)

    def _arrange_summary_cards(self, *, compact: bool) -> None:
        for frame in self._summary_frames:
            self._summary_layout.removeWidget(frame)
        columns = 2 if compact else 4
        for index, frame in enumerate(self._summary_frames):
            self._summary_layout.addWidget(frame, index // columns, index % columns)
        self._compact_summaries = compact

    def _restore_window_position(self, settings: AppSettings) -> None:
        if settings.window_x is None or settings.window_y is None:
            return
        candidate = self.frameGeometry()
        candidate.moveTopLeft(QPoint(settings.window_x, settings.window_y))
        if any(
            screen.availableGeometry().intersects(candidate) for screen in QApplication.screens()
        ):
            self.move(settings.window_x, settings.window_y)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_detail_panel"):
            self._apply_responsive_layout()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._save_settings is not None:
            geometry = self.normalGeometry()
            self._settings = replace(
                self._settings,
                window_width=max(self.MINIMUM_WIDTH, geometry.width()),
                window_height=max(self.MINIMUM_HEIGHT, geometry.height()),
                window_x=geometry.x(),
                window_y=geometry.y(),
            )
            self._save_settings(self._settings)
        if self._on_shutdown is not None and not self._shutdown_done:
            self._shutdown_done = True
            self._on_shutdown()
        super().closeEvent(event)

    @staticmethod
    def _named_label(text: str, object_name: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(object_name)
        return label
