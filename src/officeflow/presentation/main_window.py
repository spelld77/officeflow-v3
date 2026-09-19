from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from itertools import pairwise
from typing import ClassVar
from zoneinfo import ZoneInfo

from PySide6.QtCore import QModelIndex, QPoint, QSignalBlocker, Qt, QThread, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QResizeEvent, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from officeflow import __version__
from officeflow.application.attachments import AttachmentService
from officeflow.application.exporting import ExportService
from officeflow.application.migration import LegacyMigration
from officeflow.application.records import RecordService
from officeflow.application.reminders import ReminderAlert, ReminderService
from officeflow.application.tasks import (
    ScheduledTask,
    TaskGroup,
    TaskPage,
    TaskQuery,
    TaskService,
    TaskSort,
    TaskView,
)
from officeflow.domain.enums import OccurrenceStatus, ReminderRelation, TaskPriority, TaskStatus
from officeflow.domain.reminder import ReminderRuleInput
from officeflow.domain.task import Task, TaskValidationError
from officeflow.infrastructure.backup import BackupInfo, BackupManager
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.infrastructure.windows.hotkey import WindowsGlobalHotkey
from officeflow.infrastructure.windows.startup import WindowsStartupManager
from officeflow.presentation.app_icon import create_app_icon
from officeflow.presentation.data_dialog import DataManagementDialog, OperationWorker
from officeflow.presentation.help import open_user_help
from officeflow.presentation.month_calendar import CalendarPage
from officeflow.presentation.record_dialog import (
    CompleteTaskDialog,
    TaskRecordsDialog,
    WorkLogBrowserDialog,
)
from officeflow.presentation.reminder_dialog import ReminderDialog
from officeflow.presentation.settings_dialog import SettingsDialog
from officeflow.presentation.task_editor import TaskEditorDialog
from officeflow.presentation.task_list import (
    GroupHeader,
    LoadMoreRow,
    TaskItemDelegate,
    TaskListModel,
    format_task_schedule,
)
from officeflow.presentation.theme import LIGHT_STYLESHEET

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _CompletionUndo:
    task_id: int
    occurrence_start: datetime | None
    previous_status: TaskStatus
    title: str


class MainWindow(QMainWindow):
    DETAIL_BREAKPOINT = 1100
    COMPACT_BREAKPOINT = 850
    MINIMUM_WIDTH = 760
    MINIMUM_HEIGHT = 560

    VIEW_LABELS: ClassVar[dict[TaskView, tuple[str, str]]] = {
        TaskView.TODAY: ("오늘", "처리할 업무를 시간 흐름대로 보여드립니다."),
        TaskView.UPCOMING: ("예정", "오늘 이후에 시작하는 업무입니다."),
        TaskView.IMPORTANT: ("중요", "중요 또는 긴급으로 지정한 업무입니다."),
        TaskView.PENDING: ("대기", "잠시 보류한 업무입니다."),
        TaskView.COMPLETED: ("완료", "완료한 업무 기록입니다."),
        TaskView.ALL: ("전체 업무", "일정이 없는 업무를 포함한 전체 목록입니다."),
        TaskView.TRASH: (
            "휴지통",
            "삭제한 업무를 보관합니다. 첨부파일과 기록도 함께 유지됩니다.",
        ),
    }

    @property
    def tray_available(self) -> bool:
        return self._tray_icon is not None and self._tray_icon.isVisible()

    def __init__(
        self,
        settings: AppSettings,
        task_service: TaskService,
        reminder_service: ReminderService | None = None,
        record_service: RecordService | None = None,
        attachment_service: AttachmentService | None = None,
        export_service: ExportService | None = None,
        backup_manager: BackupManager | None = None,
        migration_service: LegacyMigration | None = None,
        save_settings: Callable[[AppSettings], None] | None = None,
        on_shutdown: Callable[[], None] | None = None,
        desktop_integration: bool = False,
        startup_manager: WindowsStartupManager | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._task_service = task_service
        self._reminder_service = reminder_service
        self._record_service = record_service
        self._attachment_service = attachment_service
        self._export_service = export_service
        self._backup_manager = backup_manager
        self._migration_service = migration_service
        self._save_settings = save_settings
        self._on_shutdown = on_shutdown
        self._shutdown_done = False
        self._force_quit = False
        self._desktop_integration = desktop_integration
        self._startup_manager = startup_manager or WindowsStartupManager()
        self._tray_icon: QSystemTrayIcon | None = None
        self._global_hotkey: WindowsGlobalHotkey | None = None
        self._tray_hint_shown = False
        self._current_view = TaskView.TODAY
        self._calendar_active = False
        self._selected_task_id: int | None = None
        self._selected_occurrence_start: datetime | None = None
        self._compact_navigation = False
        self._collapsed_groups = {
            group for group in TaskGroup if group.value in settings.collapsed_today_groups
        }
        self._collapsed_groups.add(TaskGroup.COMPLETED)
        self._view_preferences = {
            view: dict(preferences)
            for view, preferences in settings.view_preferences.items()
            if isinstance(preferences, dict)
        }
        self._nav_buttons: list[QPushButton] = []
        self._view_buttons: dict[TaskView, QPushButton] = {}
        self._undo_completion: _CompletionUndo | None = None
        self._undo_timer = QTimer(self)
        self._undo_timer.setSingleShot(True)
        self._undo_timer.setInterval(10_000)
        self._undo_timer.timeout.connect(self._clear_completion_undo)
        self._reminder_dialog: ReminderDialog | None = None
        self._automatic_backup_thread: QThread | None = None
        self._automatic_backup_worker: OperationWorker | None = None
        self._automatic_backup_timer = QTimer(self)
        self._automatic_backup_timer.setInterval(15 * 60 * 1_000)
        self._automatic_backup_timer.timeout.connect(self._maybe_automatic_backup)

        self.setWindowTitle("OfficeFlow v3")
        self.setWindowIcon(create_app_icon())
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
        self._undo_button = QPushButton("실행 취소")
        self._undo_button.setObjectName("undoCompletionButton")
        self._undo_button.clicked.connect(self._undo_last_completion)
        self._undo_button.hide()
        self.statusBar().addPermanentWidget(self._undo_button)
        self._restore_view_preferences(self._current_view)
        self._set_compact_list(True)
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
        self._reminder_timer = QTimer(self)
        self._reminder_timer.setInterval(30_000)
        self._reminder_timer.timeout.connect(self._check_reminders)
        if self._reminder_service is not None:
            self._reminder_timer.start()
            QTimer.singleShot(0, self._check_reminders)
        if self._desktop_integration:
            self._setup_desktop_integration()
        if self._backup_manager is not None and settings.automatic_backup_enabled:
            self._automatic_backup_timer.start()
            QTimer.singleShot(1_500, self._maybe_automatic_backup)

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
                ("휴지통", TaskView.TRASH),
            )
        ):
            self._sidebar_layout.addWidget(
                self._create_nav_button(label, view=view, selected=index == 0)
            )

        self._sidebar_layout.addSpacing(16)
        self._calendar_button = self._create_nav_button("캘린더")
        self._calendar_button.clicked.connect(self._show_calendar)
        self._calendar_button.setToolTip("월간 일정 보기")
        self._sidebar_layout.addWidget(self._calendar_button)
        self._work_log_button = self._create_nav_button("업무일지")
        self._work_log_button.setEnabled(self._record_service is not None)
        self._work_log_button.setToolTip("날짜별 업무일지 보기")
        self._work_log_button.clicked.connect(self._open_work_logs)
        self._sidebar_layout.addWidget(self._work_log_button)
        self._data_button = self._create_nav_button("데이터")
        self._data_button.setEnabled(
            self._export_service is not None and self._backup_manager is not None
        )
        self._data_button.setToolTip("내보내기·백업·복원 및 2.6 데이터 가져오기")
        self._data_button.clicked.connect(self._open_data_management)
        self._sidebar_layout.addWidget(self._data_button)
        self._help_button = self._create_nav_button("도움말")
        self._help_button.setToolTip("사용 방법과 문제 해결 안내")
        self._help_button.clicked.connect(self._open_help)
        self._sidebar_layout.addWidget(self._help_button)
        self._settings_button = self._create_nav_button("설정")
        self._settings_button.setToolTip("실행, 트레이와 알림 설정")
        self._settings_button.clicked.connect(self._open_settings)
        self._sidebar_layout.addWidget(self._settings_button)

        self._sidebar_layout.addStretch()
        self._version_label = self._named_label(f"OfficeFlow {__version__}", "brandCaption")
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
        self._content_stack = QStackedWidget()
        self._content_stack.setObjectName("workspaceStack")
        self._task_content = self._build_content()
        self._calendar_page = CalendarPage(timezone=self._settings.timezone)
        self._calendar_page.monthChanged.connect(self._refresh_calendar)
        self._calendar_page.taskSelected.connect(self._on_calendar_task_selected)
        self._calendar_page.taskActivated.connect(self._open_calendar_task)
        self._calendar_page.taskContextRequested.connect(
            self._show_calendar_task_context_menu
        )
        self._calendar_page.createRequested.connect(self._open_calendar_new)
        self._content_stack.addWidget(self._task_content)
        self._content_stack.addWidget(self._calendar_page)
        self._body_layout.addWidget(self._content_stack, 3)
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
        self._content_layout = QVBoxLayout(card)
        self._content_layout.setContentsMargins(22, 20, 22, 20)
        self._content_layout.setSpacing(14)

        heading = QHBoxLayout()
        titles = QVBoxLayout()
        self._page_title = self._named_label("오늘", "pageTitle")
        self._page_caption = self._named_label(self.VIEW_LABELS[TaskView.TODAY][1], "mutedText")
        self._page_caption.setWordWrap(False)
        titles.addWidget(self._page_title)
        titles.addWidget(self._page_caption)
        heading.addLayout(titles)
        heading.addStretch()
        self._open_selected_button = QPushButton("열기")
        self._open_selected_button.setToolTip("선택한 업무 수정")
        self._open_selected_button.clicked.connect(self._open_or_restore_selected)
        self._open_selected_button.hide()
        heading.addWidget(self._open_selected_button)
        self._open_selected_records_button = QPushButton("기록")
        self._open_selected_records_button.setObjectName("openSelectedRecordsButton")
        self._open_selected_records_button.clicked.connect(self._open_selected_records)
        self._open_selected_records_button.hide()
        heading.addWidget(self._open_selected_records_button)
        self._open_selected_attachment_button = QPushButton("첨부")
        self._open_selected_attachment_button.setObjectName("openSelectedAttachmentButton")
        self._open_selected_attachment_button.setToolTip("등록된 첨부파일 보기 및 관리")
        self._open_selected_attachment_button.clicked.connect(self._open_selected_attachments)
        self._open_selected_attachment_button.hide()
        heading.addWidget(self._open_selected_attachment_button)
        self._content_layout.addLayout(heading)
        self._content_layout.addWidget(self._build_filter_bar())

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
        self._content_layout.addLayout(quick_add)

        self._task_model = TaskListModel()
        self._task_list = QListView()
        self._task_list.setObjectName("taskList")
        self._task_list.setModel(self._task_model)
        self._task_delegate = TaskItemDelegate(self._task_list)
        self._task_list.setItemDelegate(self._task_delegate)
        self._task_delegate.quickCompleteRequested.connect(self._quick_complete_task)
        self._task_delegate.menuRequested.connect(self._show_task_menu_for_task)
        self._task_list.setMouseTracking(True)
        self._task_list.setFrameShape(QFrame.Shape.NoFrame)
        self._task_list.setSpacing(1)
        self._task_list.setUniformItemSizes(False)
        self._task_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._task_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._task_list.selectionModel().currentChanged.connect(self._on_selection_changed)
        self._task_list.clicked.connect(self._on_list_clicked)
        self._task_list.doubleClicked.connect(self._open_task_from_index)
        self._task_list.customContextMenuRequested.connect(self._show_task_context_menu)
        self._task_model.modelReset.connect(self._update_result_count)
        self._task_model.rowsInserted.connect(self._update_result_count)
        self._content_layout.addWidget(self._task_list, 1)

        self._empty_panel = QFrame()
        empty_layout = QVBoxLayout(self._empty_panel)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(
            QLabel("표시할 업무가 없습니다."), alignment=Qt.AlignmentFlag.AlignCenter
        )
        self._empty_description = self._named_label(
            "빠르게 등록하거나 다른 보기를 선택해 보세요.", "mutedText"
        )
        self._empty_description.setWordWrap(True)
        self._empty_description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self._empty_description, alignment=Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(self._empty_panel, 1)
        return card

    def _build_filter_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("filterBar")
        self._filter_bar = bar
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        primary = QHBoxLayout()
        primary.setSpacing(6)

        self._filter_toggle = QPushButton("필터")
        self._filter_toggle.setObjectName("taskFilterToggle")
        self._filter_toggle.setCheckable(True)
        self._filter_toggle.setFixedWidth(82)
        primary.addWidget(self._filter_toggle)

        self._status_filter = QComboBox()
        self._status_filter.setObjectName("statusFilter")
        self._status_filter.addItem("상태 전체", "")
        for label, status in (
            ("진행", TaskStatus.ACTIVE),
            ("대기", TaskStatus.PENDING),
            ("완료", TaskStatus.COMPLETED),
            ("취소", TaskStatus.CANCELED),
        ):
            self._status_filter.addItem(label, status.value)
        self._status_filter.setFixedWidth(92)

        self._priority_filter = QComboBox()
        self._priority_filter.setObjectName("priorityFilter")
        self._priority_filter.addItem("중요도 전체", "")
        for label, priority in (
            ("보통", TaskPriority.NORMAL),
            ("관심", TaskPriority.ATTENTION),
            ("중요", TaskPriority.IMPORTANT),
            ("긴급", TaskPriority.URGENT),
        ):
            self._priority_filter.addItem(label, priority.value)
        self._priority_filter.setFixedWidth(104)

        self._pinned_filter = QPushButton("고정")
        self._pinned_filter.setObjectName("pinnedFilter")
        self._pinned_filter.setCheckable(True)
        self._pinned_filter.setFixedWidth(58)

        self._attachment_filter = QPushButton("첨부")
        self._attachment_filter.setObjectName("attachmentFilter")
        self._attachment_filter.setCheckable(True)
        self._attachment_filter.setVisible(self._attachment_service is not None)
        self._attachment_filter.setFixedWidth(58)

        self._sort_combo = QComboBox()
        self._sort_combo.setObjectName("taskSort")
        self._sort_combo.addItem("일정순", TaskSort.SCHEDULE.value)
        self._sort_combo.addItem("중요도순", TaskSort.PRIORITY.value)
        self._sort_combo.addItem("최근 수정순", TaskSort.UPDATED.value)
        self._sort_combo.addItem("제목순", TaskSort.TITLE.value)
        self._sort_combo.setFixedWidth(92)

        self._clear_filters_button = QPushButton("모두 해제")
        self._clear_filters_button.setObjectName("clearTaskFilters")
        self._clear_filters_button.setEnabled(False)
        self._clear_filters_button.setFixedWidth(82)
        self._clear_filters_button.hide()

        self._result_count = self._named_label("", "mutedText")
        self._result_count.setObjectName("taskResultCount")
        self._active_filter_label = self._named_label("", "mutedText")
        self._active_filter_label.setObjectName("activeTaskFilters")
        primary.addWidget(self._sort_combo)
        primary.addWidget(self._active_filter_label)
        primary.addWidget(self._clear_filters_button)
        primary.addStretch()
        primary.addWidget(self._result_count)
        layout.addLayout(primary)

        self._filter_options = QFrame()
        self._filter_options.setObjectName("filterOptions")
        options = QHBoxLayout(self._filter_options)
        options.setContentsMargins(0, 0, 0, 0)
        options.setSpacing(6)
        options.addWidget(self._status_filter)
        options.addWidget(self._priority_filter)
        options.addWidget(self._pinned_filter)
        options.addWidget(self._attachment_filter)
        options.addStretch()
        self._filter_options.hide()
        layout.addWidget(self._filter_options)

        self._filter_toggle.toggled.connect(self._filter_options.setVisible)
        self._status_filter.currentIndexChanged.connect(self._on_filter_changed)
        self._priority_filter.currentIndexChanged.connect(self._on_filter_changed)
        self._pinned_filter.toggled.connect(self._on_filter_changed)
        self._attachment_filter.toggled.connect(self._on_filter_changed)
        self._sort_combo.currentIndexChanged.connect(self._on_filter_changed)
        self._clear_filters_button.clicked.connect(self._clear_filters)
        return bar

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

        self._detail_records_button = QPushButton("체크리스트 · 결과 · 업무일지")
        self._detail_records_button.setObjectName("detailRecordsButton")
        self._detail_records_button.clicked.connect(self._open_selected_records)
        self._detail_records_button.setEnabled(False)
        layout.addWidget(self._detail_records_button)
        self._detail_attachment_button = QPushButton("첨부파일 보기 · 관리")
        self._detail_attachment_button.setObjectName("detailAttachmentButton")
        self._detail_attachment_button.clicked.connect(self._open_selected_attachments)
        self._detail_attachment_button.setEnabled(False)
        layout.addWidget(self._detail_attachment_button)

        actions = QHBoxLayout()
        self._pending_button = QPushButton("대기")
        self._pending_button.clicked.connect(lambda: self._transition_selected(TaskStatus.PENDING))
        self._complete_button = QPushButton("완료")
        self._complete_button.clicked.connect(self._quick_complete_selected)
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
        self._trash_button = QPushButton("휴지통으로 이동")
        self._trash_button.setToolTip("업무와 기록, 첨부파일을 보존한 채 일반 화면에서 숨깁니다.")
        self._trash_button.clicked.connect(self._trash_or_restore_selected)
        self._trash_button.setEnabled(False)
        layout.addWidget(self._trash_button)
        return panel

    def _configure_input_tab_order(self) -> None:
        fields: tuple[QWidget, ...] = (
            self._search,
            self._add_button,
            self._filter_toggle,
            self._sort_combo,
            self._status_filter,
            self._priority_filter,
            self._pinned_filter,
            self._attachment_filter,
            self._clear_filters_button,
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
        self._remember_view_preferences()
        self._calendar_active = False
        self._current_view = view
        self._content_stack.setCurrentWidget(self._task_content)
        self._restore_view_preferences(view)
        title, caption = self.VIEW_LABELS[view]
        self._page_title.setText(title)
        self._page_caption.setText(caption)
        for button_view, button in self._view_buttons.items():
            button.setProperty("selected", button_view is view)
            button.style().unpolish(button)
            button.style().polish(button)
        self._set_nav_selected(self._calendar_button, False)
        self._selected_task_id = None
        self._selected_occurrence_start = None
        trash = view is TaskView.TRASH
        self._quick_add_edit.setVisible(not trash)
        self._quick_add_button.setVisible(not trash)
        self._apply_responsive_layout()
        self._refresh_tasks()

    def _show_calendar(self) -> None:
        self._remember_view_preferences()
        self._calendar_active = True
        self._content_stack.setCurrentWidget(self._calendar_page)
        for button in self._view_buttons.values():
            self._set_nav_selected(button, False)
        self._set_nav_selected(self._calendar_button, True)
        self._selected_task_id = None
        self._selected_occurrence_start = None
        self._update_detail(None)
        self._apply_responsive_layout()
        self._refresh_calendar()

    def _refresh_tasks(self) -> None:
        if self._calendar_active:
            self._refresh_calendar()
            return
        try:
            selected_id = self._selected_task_id
            if self._current_view is TaskView.TODAY:
                local_day = datetime.now(ZoneInfo(self._settings.timezone)).date()
                self._page_title.setText(
                    f"오늘 · {local_day.month}월 {local_day.day}일"
                )
                pages = self._task_service.today_flow_pages(
                    self._build_query(offset=0, group=None),
                )
                self._task_model.set_group_pages(
                    pages,
                    collapsed=frozenset(self._collapsed_groups),
                    loader=self._load_group_page,
                )
                total = sum(page.total for page in pages.values())
                remaining = (
                    pages[TaskGroup.OVERDUE].total
                    + pages[TaskGroup.IN_PROGRESS].total
                )
                completed = pages[TaskGroup.COMPLETED].total
                self._page_caption.setText(
                    f"남은 업무 {remaining}개 · 완료 {completed}개"
                )
            else:
                self._page_title.setText(self.VIEW_LABELS[self._current_view][0])
                page = self._task_service.query(self._build_query(offset=0))
                self._task_model.set_page(page, self._load_flat_page)
                total = page.total
            self._task_list.setVisible(total > 0)
            self._empty_panel.setVisible(total == 0)
            self._empty_description.setText(
                "필터 조건에 맞는 업무가 없습니다. 위의 '해제'를 누르면 전체 업무를 볼 수 있습니다."
                if total == 0 and self._active_filter_count()
                else "빠르게 등록하거나 다른 보기를 선택해 보세요."
            )
            self._update_result_count()

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

    def _refresh_calendar(self, _year: int | None = None, _month: int | None = None) -> None:
        try:
            start_date, end_date = self._calendar_page.visible_date_range
            tasks = self._task_service.calendar_schedule(
                start_date,
                end_date,
                search=self._search.text(),
            )
            self._calendar_page.set_tasks(tasks)
            self.statusBar().showMessage(f"캘린더 일정 {len(tasks)}개", 1800)
        except Exception as error:
            logger.exception("Failed to refresh calendar")
            self.statusBar().showMessage(f"캘린더를 불러오지 못했습니다: {error}", 5000)

    def _build_query(
        self,
        *,
        offset: int,
        limit: int | None = TaskListModel.PAGE_SIZE,
        group: TaskGroup | None = None,
    ) -> TaskQuery:
        return TaskQuery(
            view=self._current_view,
            search=self._search.text(),
            statuses=self._selected_statuses(),
            priorities=self._selected_priorities(),
            pinned_only=self._pinned_filter.isChecked(),
            has_attachments=(True if self._attachment_filter.isChecked() else None),
            group=group,
            sort=self._selected_sort(),
            offset=offset,
            limit=limit,
        )

    def _load_flat_page(self, offset: int, limit: int) -> TaskPage:
        return self._task_service.query(self._build_query(offset=offset, limit=limit))

    def _load_group_page(self, group: TaskGroup, offset: int, limit: int) -> TaskPage:
        return self._task_service.today_flow_page(
            self._build_query(offset=offset, limit=limit, group=None),
            group,
        )

    def _selected_statuses(self) -> frozenset[TaskStatus]:
        value = str(self._status_filter.currentData())
        return frozenset({TaskStatus(value)}) if value else frozenset()

    def _selected_priorities(self) -> frozenset[TaskPriority]:
        value = str(self._priority_filter.currentData())
        return frozenset({TaskPriority(value)}) if value else frozenset()

    def _selected_sort(self) -> TaskSort:
        return TaskSort(str(self._sort_combo.currentData()))

    def _on_filter_changed(self, _value: object = None) -> None:
        self._remember_view_preferences()
        self._update_filter_feedback()
        self._selected_task_id = None
        self._refresh_tasks()

    def _clear_filters(self) -> None:
        blockers = (
            QSignalBlocker(self._status_filter),
            QSignalBlocker(self._priority_filter),
            QSignalBlocker(self._pinned_filter),
            QSignalBlocker(self._attachment_filter),
        )
        self._status_filter.setCurrentIndex(0)
        self._priority_filter.setCurrentIndex(0)
        self._pinned_filter.setChecked(False)
        self._attachment_filter.setChecked(False)
        del blockers
        self._update_filter_feedback()
        self._on_filter_changed()

    def _set_compact_list(self, compact: bool) -> None:
        self._task_delegate.set_compact(compact)
        self._task_list.setSpacing(0 if compact else 1)
        self._task_list.doItemsLayout()
        self._task_list.viewport().update()

    def _remember_view_preferences(self) -> None:
        if not hasattr(self, "_status_filter"):
            return
        self._view_preferences[self._current_view.value] = {
            "status": str(self._status_filter.currentData()),
            "priority": str(self._priority_filter.currentData()),
            "pinned_only": self._pinned_filter.isChecked(),
            "has_attachments": self._attachment_filter.isChecked(),
            "sort": str(self._sort_combo.currentData()),
        }

    def _restore_view_preferences(self, view: TaskView) -> None:
        preferences = self._view_preferences.get(view.value, {})
        blockers = (
            QSignalBlocker(self._status_filter),
            QSignalBlocker(self._priority_filter),
            QSignalBlocker(self._pinned_filter),
            QSignalBlocker(self._attachment_filter),
            QSignalBlocker(self._sort_combo),
        )
        self._set_combo_value(self._status_filter, str(preferences.get("status", "")))
        self._set_combo_value(self._priority_filter, str(preferences.get("priority", "")))
        self._pinned_filter.setChecked(bool(preferences.get("pinned_only", False)))
        self._attachment_filter.setChecked(bool(preferences.get("has_attachments", False)))
        default_sort = (
            TaskSort.UPDATED.value if view is TaskView.TRASH else TaskSort.SCHEDULE.value
        )
        self._set_combo_value(
            self._sort_combo,
            str(preferences.get("sort", default_sort)),
        )
        del blockers
        self._update_filter_feedback()

    def _active_filter_count(self) -> int:
        return sum(
            (
                bool(self._selected_statuses()),
                bool(self._selected_priorities()),
                self._pinned_filter.isChecked(),
                self._attachment_filter.isChecked(),
            )
        )

    def _update_filter_feedback(self) -> None:
        pinned = self._pinned_filter.isChecked()
        attached = self._attachment_filter.isChecked()
        self._pinned_filter.setText("✓고정" if pinned else "고정")
        self._attachment_filter.setText("✓첨부" if attached else "첨부")
        self._pinned_filter.setToolTip(
            "고정된 업무만 표시 중" if pinned else "고정된 업무만 표시"
        )
        self._attachment_filter.setToolTip(
            "첨부파일이 있는 업무만 표시 중" if attached else "첨부파일이 있는 업무만 표시"
        )
        self._clear_filters_button.setEnabled(self._active_filter_count() > 0)
        active = self._active_filter_count()
        self._clear_filters_button.setVisible(active > 0)
        self._filter_toggle.setText(f"필터 {active}개" if active else "필터")
        labels: list[str] = []
        if self._selected_statuses():
            labels.append(self._status_filter.currentText())
        if self._selected_priorities():
            labels.append(self._priority_filter.currentText())
        if pinned:
            labels.append("고정")
        if attached:
            labels.append("첨부 있음")
        self._active_filter_label.setText(" · ".join(labels))

    def _on_list_clicked(self, index: QModelIndex) -> None:
        entry = self._task_model.entry_at(index)
        if isinstance(entry, GroupHeader):
            collapsed = self._task_model.toggle_group(entry.group)
            if collapsed:
                self._collapsed_groups.add(entry.group)
            else:
                self._collapsed_groups.discard(entry.group)
            return
        if isinstance(entry, LoadMoreRow):
            self._task_model.load_more(entry.group)

    def _show_task_context_menu(self, position: QPoint) -> None:
        index = self._task_list.indexAt(position)
        task = self._task_model.task_at(index)
        if task is None:
            return
        self._task_list.setCurrentIndex(index)
        menu = self._build_task_context_menu(task)
        menu.exec(self._task_list.viewport().mapToGlobal(position))

    def _show_task_menu_for_task(self, task: Task, global_position: QPoint) -> None:
        self._select_task_for_action(task)
        self._build_task_context_menu(task).exec(global_position)

    def _select_task_for_action(self, task: Task) -> None:
        if task.id is None:
            return
        index = self._task_model.index_for_task(task.id)
        if index.isValid():
            self._task_list.setCurrentIndex(index)
        self._selected_task_id = task.id
        self._selected_occurrence_start = (
            task.starts_at
            if task.deleted_at is None and task.recurrence_rule
            else None
        )

    def _build_task_context_menu(self, task: Task) -> QMenu:
        menu = QMenu(self)
        if task.deleted_at is not None:
            if self._attachment_service is not None and self._record_service is not None:
                attachments = menu.addAction("첨부파일 보기…")
                attachments.triggered.connect(self._open_selected_attachments)
            if self._record_service is not None:
                records = menu.addAction("결과 · 기록 보기")
                records.triggered.connect(self._open_selected_records)
            if menu.actions():
                menu.addSeparator()
            restore = menu.addAction("휴지통에서 복원")
            restore.triggered.connect(self._restore_selected_from_trash)
            return menu
        if task.status in {TaskStatus.ACTIVE, TaskStatus.PENDING}:
            complete_now = menu.addAction("바로 완료")
            complete_now.triggered.connect(self._quick_complete_selected)
            complete_with_result = menu.addAction("결과 입력 후 완료…")
            complete_with_result.triggered.connect(self._complete_selected_with_result)
        elif task.status is TaskStatus.COMPLETED:
            edit_result = menu.addAction("결과 입력 · 수정…")
            edit_result.triggered.connect(self._open_selected_result)
            if self._selected_occurrence_start is None:
                reactivate = menu.addAction("다시 진행")
                reactivate.triggered.connect(
                    lambda: self._transition_selected(TaskStatus.ACTIVE)
                )

        if menu.actions():
            menu.addSeparator()
        if self._attachment_service is not None and self._record_service is not None:
            attachments = menu.addAction("첨부파일 보기 · 관리…")
            attachments.triggered.connect(self._open_selected_attachments)
        if self._record_service is not None:
            records = menu.addAction("결과 · 기록 열기")
            records.triggered.connect(self._open_selected_records)
        edit = menu.addAction("업무 수정")
        edit.triggered.connect(self._open_selected_task)
        menu.addSeparator()
        delete = menu.addAction("휴지통으로 이동")
        delete.triggered.connect(self._move_selected_to_trash)
        return menu

    def _quick_complete_task(self, task: Task) -> None:
        self._select_task_for_action(task)
        self._quick_complete_selected()

    def _quick_complete_selected(self) -> None:
        if self._selected_task_id is None:
            return
        try:
            task = self._task_service.get(self._selected_task_id)
        except LookupError as error:
            self._show_error("업무를 완료하지 못했습니다.", error)
            return
        previous_status = task.status
        undo = _CompletionUndo(
            task_id=self._selected_task_id,
            occurrence_start=self._selected_occurrence_start,
            previous_status=previous_status,
            title=task.title,
        )
        if not self._transition_selected(TaskStatus.COMPLETED):
            return
        self._undo_completion = undo
        self._undo_button.show()
        self._undo_timer.start()
        target = "현재 반복 일정을" if undo.occurrence_start is not None else "업무를"
        self.statusBar().showMessage(
            f"'{undo.title}' {target} 완료했습니다.",
            10_000,
        )

    def _undo_last_completion(self) -> None:
        undo = self._undo_completion
        if undo is None:
            return
        try:
            if undo.occurrence_start is not None:
                self._task_service.transition_occurrence(
                    undo.task_id,
                    undo.occurrence_start,
                    OccurrenceStatus.PENDING,
                )
            else:
                self._task_service.transition(undo.task_id, TaskStatus.ACTIVE)
                if undo.previous_status is TaskStatus.PENDING:
                    self._task_service.transition(undo.task_id, TaskStatus.PENDING)
        except (TaskValidationError, LookupError, ValueError) as error:
            self._show_error("완료를 되돌리지 못했습니다.", error)
            return
        self._clear_completion_undo()
        self._refresh_tasks()
        self.statusBar().showMessage(f"'{undo.title}' 완료를 취소했습니다.", 4000)

    def _clear_completion_undo(self) -> None:
        self._undo_timer.stop()
        self._undo_completion = None
        if hasattr(self, "_undo_button"):
            self._undo_button.hide()

    def _update_result_count(self, *_args: object) -> None:
        if not hasattr(self, "_result_count"):
            return
        loaded = self._task_model.loaded_task_count
        total = self._task_model.total_task_count
        count_text = f"{loaded}/{total}개 표시" if loaded < total else f"총 {total}개"
        active_filters = self._active_filter_count()
        self._result_count.setText(
            f"필터 {active_filters}개 · {count_text}" if active_filters else count_text
        )

    @staticmethod
    def _set_combo_value(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

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
        initial_date = self._calendar_page.selected_date if self._calendar_active else None
        self._open_editor(None, initial_date=initial_date)

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self._settings, self)
        dialog.setStyleSheet(LIGHT_STYLESHEET)
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            return
        previous = self._settings
        updated = dialog.settings()
        try:
            self._startup_manager.set_enabled(updated.start_with_windows)
        except OSError as error:
            self._show_error("Windows 시작 프로그램 설정을 변경하지 못했습니다.", error)
            updated = replace(
                updated,
                start_with_windows=self._settings.start_with_windows,
            )
        self._settings = updated
        self._sync_quit_policy()
        shortcut_registered = self._register_global_hotkey()
        if self._desktop_integration and not shortcut_registered:
            self._settings = replace(
                self._settings,
                global_quick_add_shortcut=previous.global_quick_add_shortcut,
            )
            self._register_global_hotkey()
        if self._save_settings is not None:
            self._save_settings(self._settings)
        if shortcut_registered or not self._desktop_integration:
            self.statusBar().showMessage("설정을 적용했습니다.", 3000)
        else:
            self.statusBar().showMessage(
                "설정은 저장했지만 전역 단축키를 등록하지 못했습니다.", 5000
            )
        if self._settings.automatic_backup_enabled:
            self._automatic_backup_timer.start()
            QTimer.singleShot(0, self._maybe_automatic_backup)
        else:
            self._automatic_backup_timer.stop()

    def _open_data_management(self) -> None:
        if self._export_service is None or self._backup_manager is None:
            return
        dialog = DataManagementDialog(
            export_service=self._export_service,
            backup_manager=self._backup_manager,
            query=self._build_query(offset=0, limit=None),
            migration_service=self._migration_service,
            attachment_service=self._attachment_service,
            task_service=self._task_service,
            parent=self,
        )
        dialog.quitRequested.connect(self._quit_application)
        dialog.setStyleSheet(LIGHT_STYLESHEET)
        dialog.exec()

    def _open_help(self) -> None:
        if not open_user_help():
            QMessageBox.warning(
                self,
                "도움말을 열 수 없습니다.",
                "사용자 안내 파일을 찾지 못했습니다. OfficeFlow를 다시 설치하세요.",
            )

    def _maybe_automatic_backup(self) -> None:
        if (
            self._backup_manager is None
            or not self._settings.automatic_backup_enabled
            or self._automatic_backup_thread is not None
        ):
            return
        try:
            required = self._backup_manager.should_create_automatic_backup(
                self._settings.automatic_backup_interval_hours
            )
        except OSError:
            logger.exception("자동 백업 시점을 확인하지 못했습니다.")
            return
        if not required:
            return
        manager = self._backup_manager
        keep = self._settings.automatic_backup_keep
        thread = QThread(self)
        worker = OperationWorker(
            lambda canceled: manager.create_backup(
                reason="automatic", keep=keep, cancel_requested=canceled
            )
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._automatic_backup_succeeded)
        worker.failed.connect(self._automatic_backup_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._automatic_backup_finished)
        self._automatic_backup_thread = thread
        self._automatic_backup_worker = worker
        thread.start()

    def _automatic_backup_succeeded(self, result: object) -> None:
        if isinstance(result, BackupInfo):
            self.statusBar().showMessage(
                f"자동 백업을 완료했습니다: {result.path.name}", 4000
            )

    @staticmethod
    def _automatic_backup_failed(error: object) -> None:
        logger.error("자동 백업에 실패했습니다: %s", error)

    def _automatic_backup_finished(self) -> None:
        self._automatic_backup_worker = None
        self._automatic_backup_thread = None

    def _open_selected_task(self) -> None:
        if self._selected_task_id is None:
            return
        try:
            self._open_editor(self._task_service.get(self._selected_task_id))
        except Exception as error:
            self._show_error("업무를 열지 못했습니다.", error)

    def _open_or_restore_selected(self) -> None:
        if self._current_view is TaskView.TRASH:
            self._restore_selected_from_trash()
            return
        self._open_selected_task()

    def _trash_or_restore_selected(self) -> None:
        if self._current_view is TaskView.TRASH:
            self._restore_selected_from_trash()
            return
        self._move_selected_to_trash()

    def _move_selected_to_trash(self) -> None:
        if self._selected_task_id is None:
            return
        try:
            task = self._task_service.get(self._selected_task_id)
        except LookupError as error:
            self._show_error("업무를 휴지통으로 이동하지 못했습니다.", error)
            return
        title = task.title
        detail = (
            f"'{title}' 업무를 휴지통으로 이동할까요?\n\n"
            "일반 목록, 캘린더와 알림에서는 사라지지만 기록과 첨부파일은 보존됩니다."
        )
        if task.recurrence_rule:
            detail += "\n반복 일정 전체가 휴지통으로 이동합니다."
        answer = QMessageBox.question(
            self,
            "업무 삭제",
            detail,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._task_service.move_to_trash(self._selected_task_id)
        except (LookupError, ValueError) as error:
            self._show_error("업무를 휴지통으로 이동하지 못했습니다.", error)
            return
        self._selected_task_id = None
        self._selected_occurrence_start = None
        self._update_detail(None)
        self._refresh_tasks()
        self.statusBar().showMessage(f"'{title}' 업무를 휴지통으로 이동했습니다.", 4000)

    def _restore_selected_from_trash(self) -> None:
        if self._selected_task_id is None:
            return
        task = self._task_model.task_at(self._task_list.currentIndex())
        title = task.title if task is not None else "선택한 업무"
        try:
            self._task_service.restore_from_trash(self._selected_task_id)
        except (LookupError, ValueError) as error:
            self._show_error("업무를 복원하지 못했습니다.", error)
            return
        self._selected_task_id = None
        self._selected_occurrence_start = None
        self._update_detail(None)
        self._refresh_tasks()
        self.statusBar().showMessage(f"'{title}' 업무를 복원했습니다.", 4000)

    def _open_selected_records(
        self,
        *,
        initial_tab: str | None = None,
    ) -> None:
        if self._selected_task_id is None or self._record_service is None:
            return
        try:
            task = self._task_service.get_including_deleted(self._selected_task_id)
            dialog = TaskRecordsDialog(
                task,
                task_service=self._task_service,
                record_service=self._record_service,
                attachment_service=self._attachment_service,
                occurrence_start=self._selected_occurrence_start,
                initial_tab=initial_tab,
                parent=self,
            )
            dialog.setStyleSheet(LIGHT_STYLESHEET)
            dialog.changed.connect(self._refresh_tasks)
            dialog.exec()
        except Exception as error:
            self._show_error("업무 기록을 열지 못했습니다.", error)

    def _open_selected_result(self) -> None:
        self._open_selected_records(initial_tab="result")

    def _open_selected_attachments(self) -> None:
        self._open_selected_records(initial_tab="attachments")

    def _complete_selected_with_result(self) -> None:
        if self._selected_task_id is None:
            return
        try:
            task = self._task_service.get(self._selected_task_id)
            current_result = self._task_service.result_note(
                self._selected_task_id,
                self._selected_occurrence_start,
            )
        except (LookupError, ValueError) as error:
            self._show_error("업무를 열지 못했습니다.", error)
            return
        dialog = CompleteTaskDialog(
            task,
            result_note=current_result,
            parent=self,
        )
        dialog.setStyleSheet(LIGHT_STYLESHEET)
        if dialog.exec() != CompleteTaskDialog.DialogCode.Accepted:
            return
        self._transition_selected(TaskStatus.COMPLETED, result_note=dialog.result_note())

    def _open_work_logs(self) -> None:
        if self._record_service is None:
            return
        dialog = WorkLogBrowserDialog(
            task_service=self._task_service,
            record_service=self._record_service,
            attachment_service=self._attachment_service,
            parent=self,
        )
        dialog.setStyleSheet(LIGHT_STYLESHEET)
        dialog.exec()

    def _open_task_from_index(self, index: QModelIndex) -> None:
        task = self._task_model.task_at(index)
        if task is not None:
            self._selected_task_id = task.id
            if task.deleted_at is not None:
                self.statusBar().showMessage(
                    "휴지통의 업무는 먼저 복원한 뒤 열 수 있습니다.", 3500
                )
                return
            self._open_editor(
                self._task_service.get(task.id)
                if task.recurrence_rule and task.id is not None
                else task
            )

    def _open_editor(self, task: Task | None, *, initial_date: date | None = None) -> None:
        reminder_rules = (
            self._reminder_service.rules_for_task(task.id)
            if self._reminder_service is not None and task is not None and task.id is not None
            else ()
        )
        editor = TaskEditorDialog(
            timezone=self._settings.timezone,
            task=task,
            reminder_rules=reminder_rules,
            initial_date=initial_date,
            parent=self,
        )
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
        self._selected_occurrence_start = None
        if self._reminder_service is not None and saved.id is not None:
            try:
                self._reminder_service.replace_rules(saved.id, editor.draft().reminder_rules)
            except Exception as error:
                self._show_error("알림 규칙을 저장하지 못했습니다.", error)
                return
        self._refresh_tasks()
        self.statusBar().showMessage("업무를 저장했습니다.", 2500)

    def _open_calendar_new(self, selected_date: date) -> None:
        self._open_editor(None, initial_date=selected_date)

    def _open_calendar_task(self, scheduled: ScheduledTask) -> None:
        self._selected_task_id = scheduled.id
        if scheduled.id is None:
            return
        self._open_editor(self._task_service.get(scheduled.id))

    def _on_calendar_task_selected(self, scheduled: ScheduledTask) -> None:
        self._selected_task_id = scheduled.id
        self._selected_occurrence_start = scheduled.occurrence_start
        self._update_detail(scheduled.display_task)
        self._apply_responsive_layout()

    def _show_calendar_task_context_menu(
        self,
        scheduled: ScheduledTask,
        global_position: QPoint,
    ) -> None:
        self._on_calendar_task_selected(scheduled)
        menu = self._build_task_context_menu(scheduled.display_task)
        menu.exec(global_position)

    def _on_selection_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        task = self._task_model.task_at(current)
        self._selected_task_id = task.id if task else None
        self._selected_occurrence_start = (
            task.starts_at
            if task is not None and task.deleted_at is None and task.recurrence_rule
            else None
        )
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
                self._detail_records_button,
                self._detail_attachment_button,
                self._trash_button,
            ):
                button.setEnabled(False)
            self._open_selected_button.hide()
            self._open_selected_records_button.hide()
            self._open_selected_attachment_button.hide()
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
        deleted = task.deleted_at is not None
        self._detail_status.setText(
            "휴지통 · 원래 상태 " + status_labels[task.status]
            if deleted
            else f"{status_labels[task.status]} · 중요도 {priority_labels[task.priority.value]}"
        )
        self._detail_schedule.setText(format_task_schedule(task))
        if deleted and task.deleted_at is not None:
            deleted_local = task.deleted_at.astimezone(ZoneInfo(self._settings.timezone))
            self._detail_schedule.setText(
                f"{self._detail_schedule.text()}\n삭제: {deleted_local:%Y.%m.%d %H:%M}"
            )
        if task.has_attachments:
            self._detail_schedule.setText(f"{self._detail_schedule.text()}\n첨부파일 있음")
        if task.recurrence_rule:
            self._detail_schedule.setText(f"{self._detail_schedule.text()}\n반복 일정")
        if not deleted and self._reminder_service is not None and task.id is not None:
            try:
                reminder_rules = self._reminder_service.rules_for_task(task.id)
            except Exception:
                logger.exception("업무 알림 규칙을 읽지 못했습니다.")
            else:
                if reminder_rules:
                    reminder_text = " · ".join(
                        self._format_reminder_rule(rule) for rule in reminder_rules
                    )
                    self._detail_schedule.setText(
                        f"{self._detail_schedule.text()}\n알림: {reminder_text}"
                    )
        self._detail_description.setText(task.description or "설명이 없습니다.")
        self._edit_button.setEnabled(not deleted)
        self._detail_records_button.setEnabled(self._record_service is not None)
        self._detail_attachment_button.setEnabled(
            self._record_service is not None
            and self._attachment_service is not None
        )
        self._pending_button.setEnabled(not deleted and task.status is TaskStatus.ACTIVE)
        self._complete_button.setEnabled(
            not deleted and task.status in {TaskStatus.ACTIVE, TaskStatus.PENDING}
        )
        self._archive_button.setEnabled(not deleted and task.status is not TaskStatus.ARCHIVED)
        self._trash_button.setText("복원" if deleted else "휴지통으로 이동")
        self._trash_button.setEnabled(True)
        if self._selected_occurrence_start is not None:
            self._pending_button.setText("건너뛰기")
            self._pending_button.setEnabled(task.status is not TaskStatus.COMPLETED)
            self._archive_button.setEnabled(False)
        else:
            self._pending_button.setText("대기")
        self._open_selected_button.setText("복원" if deleted else "열기")
        self._open_selected_button.setToolTip(
            "선택한 업무 복원" if deleted else "선택한 업무 수정"
        )
        self._open_selected_button.setVisible(not self._detail_panel.isVisible())
        self._open_selected_records_button.setVisible(
            self._record_service is not None and not self._detail_panel.isVisible()
        )
        self._open_selected_attachment_button.setVisible(
            self._record_service is not None
            and self._attachment_service is not None
            and not self._detail_panel.isVisible()
        )

    def _transition_selected(
        self,
        status: TaskStatus,
        *,
        result_note: str | None = None,
    ) -> bool:
        if self._selected_task_id is None:
            return False
        try:
            if self._selected_occurrence_start is not None:
                occurrence_status = (
                    OccurrenceStatus.COMPLETED
                    if status is TaskStatus.COMPLETED
                    else OccurrenceStatus.SKIPPED
                )
                self._task_service.transition_occurrence(
                    self._selected_task_id,
                    self._selected_occurrence_start,
                    occurrence_status,
                    result_note=result_note,
                )
                if occurrence_status is OccurrenceStatus.SKIPPED:
                    self._selected_occurrence_start = None
                    self._selected_task_id = None
                    self._update_detail(None)
            else:
                self._task_service.transition(self._selected_task_id, status)
                if result_note is not None:
                    self._task_service.update_result_note(
                        self._selected_task_id,
                        result_note,
                    )
        except (TaskValidationError, LookupError, ValueError) as error:
            self._show_error("상태를 변경하지 못했습니다.", error)
            return False
        self._refresh_tasks()
        self.statusBar().showMessage("업무 상태를 변경했습니다.", 2500)
        return True

    def _check_reminders(self) -> None:
        if self._reminder_service is None:
            return
        try:
            alerts = self._reminder_service.poll_due(
                grace_minutes=self._settings.missed_reminder_grace_minutes
            )
        except Exception:
            logger.exception("알림을 확인하지 못했습니다.")
            self.statusBar().showMessage("알림 확인 중 문제가 발생했습니다.", 4000)
            return
        if not alerts:
            return
        self._show_system_reminder(alerts)
        if self._reminder_dialog is not None:
            self._reminder_dialog.add_alerts(alerts)
            self._reminder_dialog.present()
            return
        self._reminder_dialog = ReminderDialog(
            alerts,
            timezone=self._settings.timezone,
            parent=None,
        )
        self._reminder_dialog.setWindowIcon(self.windowIcon())
        self._reminder_dialog.setStyleSheet(LIGHT_STYLESHEET)
        self._reminder_dialog.actionRequested.connect(self._handle_reminder_action)
        self._reminder_dialog.snoozeRequested.connect(
            lambda delivery_id, minutes: self._handle_reminder_action(
                delivery_id,
                f"snooze:{minutes}",
            )
        )
        self._reminder_dialog.finished.connect(lambda _result: self._clear_reminder_dialog())
        self._reminder_dialog.present()

    def _show_system_reminder(self, alerts: tuple[ReminderAlert, ...]) -> None:
        if self._tray_icon is None or not self._tray_icon.isVisible():
            return
        if len(alerts) == 1:
            alert = alerts[0]
            title = "OfficeFlow 업무 알림"
            body = alert.task.title
        else:
            title = f"OfficeFlow 알림 {len(alerts)}개"
            body = "놓친 알림과 예정된 업무를 한 번에 확인하세요."
        self._tray_icon.showMessage(
            title,
            body,
            QSystemTrayIcon.MessageIcon.Information,
            8_000,
        )

    def _handle_reminder_action(self, delivery_id: int, action: str) -> None:
        if self._reminder_service is None:
            return
        snoozed_until: datetime | None = None
        action_name = action
        try:
            if action == "complete":
                self._reminder_service.complete(delivery_id)
            elif action == "defer":
                self._reminder_service.defer(delivery_id)
            elif action.startswith("snooze"):
                parts = action.split(":", maxsplit=1)
                minutes = int(parts[1]) if len(parts) == 2 else 10
                delivery = self._reminder_service.snooze(delivery_id, minutes)
                snoozed_until = delivery.snoozed_until
                action_name = "snooze"
            else:
                self._reminder_service.acknowledge(delivery_id)
        except Exception as error:
            self._show_error("알림 작업을 처리하지 못했습니다.", error)
            return
        if self._reminder_dialog is not None:
            self._reminder_dialog.remove_delivery(delivery_id)
        self._refresh_tasks()
        messages = {
            "complete": "업무를 완료했습니다.",
            "defer": "업무를 대기로 전환했습니다.",
            "acknowledge": "알림을 확인했습니다.",
        }
        if action_name == "snooze" and snoozed_until is not None:
            local_due = snoozed_until.astimezone(ZoneInfo(self._settings.timezone))
            message = f"다시 알림이 {local_due:%m월 %d일 %H:%M}로 설정되었습니다."
            self.statusBar().showMessage(message, 10_000)
            if self._tray_icon is not None and self._tray_icon.isVisible():
                self._tray_icon.showMessage(
                    "OfficeFlow 다시 알림",
                    message,
                    QSystemTrayIcon.MessageIcon.Information,
                    8_000,
                )
        else:
            self.statusBar().showMessage(
                messages.get(action_name, "알림을 처리했습니다."), 3000
            )

    def _clear_reminder_dialog(self) -> None:
        self._reminder_dialog = None

    def _setup_desktop_integration(self) -> None:
        application = QApplication.instance()
        if application is None:
            return
        if QSystemTrayIcon.isSystemTrayAvailable():
            tray = QSystemTrayIcon(self.windowIcon(), self)
            tray.setToolTip("OfficeFlow v3")
            menu = QMenu(self)
            open_action = QAction("OfficeFlow 열기", menu)
            open_action.triggered.connect(self._show_from_tray)
            quick_add_action = QAction("빠른 업무 등록", menu)
            quick_add_action.triggered.connect(self._show_global_quick_add)
            quit_action = QAction("완전히 종료", menu)
            quit_action.triggered.connect(self._quit_application)
            menu.addAction(open_action)
            menu.addAction(quick_add_action)
            menu.addSeparator()
            menu.addAction(quit_action)
            tray.setContextMenu(menu)
            tray.activated.connect(self._on_tray_activated)
            tray.messageClicked.connect(self._show_from_tray)
            tray.show()
            self._tray_icon = tray
            self._sync_quit_policy()
        try:
            self._startup_manager.set_enabled(self._settings.start_with_windows)
        except OSError:
            logger.exception("Windows 시작 프로그램 설정을 동기화하지 못했습니다.")
        if not self._register_global_hotkey():
            self.statusBar().showMessage(
                "전역 빠른 등록 단축키를 등록하지 못했습니다. 설정에서 변경하세요.",
                6000,
            )

    def _register_global_hotkey(self) -> bool:
        if self._global_hotkey is not None:
            self._global_hotkey.stop()
            self._global_hotkey = None
        if not self._desktop_integration:
            return False
        application = QApplication.instance()
        if application is None:
            return False
        try:
            hotkey = WindowsGlobalHotkey(
                application,
                self._settings.global_quick_add_shortcut,
                self._show_global_quick_add,
            )
            registered = hotkey.start()
        except (OSError, ValueError):
            logger.exception("전역 빠른 등록 단축키를 등록하지 못했습니다.")
            return False
        self._global_hotkey = hotkey
        return registered

    def _sync_quit_policy(self) -> None:
        application = QApplication.instance()
        if isinstance(application, QApplication):
            keep_running = self.tray_available and self._settings.minimize_to_tray
            application.setQuitOnLastWindowClosed(not keep_running)

    def handle_external_command(self, command: str) -> None:
        if command.strip().casefold() == "quick-add":
            self._show_global_quick_add()
        else:
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        if self._reminder_dialog is not None:
            self._reminder_dialog.show()
            self._reminder_dialog.raise_()
            self._reminder_dialog.activateWindow()

    def _show_global_quick_add(self) -> None:
        self._show_from_tray()
        self._set_view(TaskView.ALL)
        self._quick_add_edit.setFocus()
        self._quick_add_edit.selectAll()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self._show_from_tray()

    def _quit_application(self) -> None:
        self._force_quit = True
        self.close()
        application = QApplication.instance()
        if application is not None:
            application.quit()

    def shutdown(self) -> None:
        self._persist_settings()
        self._automatic_backup_timer.stop()
        if self._reminder_dialog is not None:
            self._reminder_dialog.dismiss_for_shutdown()
            self._reminder_dialog = None
        if self._automatic_backup_worker is not None:
            self._automatic_backup_worker.cancel()
        if self._automatic_backup_thread is not None:
            self._automatic_backup_thread.quit()
            self._automatic_backup_thread.wait()
        if self._global_hotkey is not None:
            self._global_hotkey.stop()
            self._global_hotkey = None
        if self._tray_icon is not None:
            self._tray_icon.hide()
        if self._on_shutdown is not None and not self._shutdown_done:
            self._shutdown_done = True
            self._on_shutdown()

    @staticmethod
    def _format_reminder_rule(rule: ReminderRuleInput) -> str:
        relation = {
            ReminderRelation.START: "시작",
            ReminderRelation.END: "종료",
            ReminderRelation.ABSOLUTE: "지정",
        }[rule.relation]
        if rule.absolute_at is not None:
            return f"{relation} {rule.absolute_at:%Y-%m-%d %H:%M}"
        offset = rule.offset_minutes or 0
        if offset == 0:
            timing = "시각"
        elif offset == 540 and rule.relation is ReminderRelation.START:
            timing = "당일 오전 9시"
        elif offset < 0:
            minutes = abs(offset)
            timing = f"{minutes // 1_440}일 전" if minutes % 1_440 == 0 else (
                f"{minutes // 60}시간 전" if minutes % 60 == 0 else f"{minutes}분 전"
            )
        else:
            timing = f"{offset}분 후"
        return f"{relation} {timing}"

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
            not self._calendar_active and not show_detail and self._selected_task_id is not None
        )
        self._open_selected_records_button.setVisible(
            self._record_service is not None
            and not self._calendar_active
            and not show_detail
            and self._selected_task_id is not None
        )
        self._open_selected_attachment_button.setVisible(
            self._record_service is not None
            and self._attachment_service is not None
            and not self._calendar_active
            and not show_detail
            and self._selected_task_id is not None
        )
        self._calendar_page.set_compact(compact_navigation)

        if compact_navigation:
            self._sidebar.setFixedWidth(88)
            self._sidebar_layout.setContentsMargins(10, 20, 10, 16)
            self._workspace_layout.setContentsMargins(12, 12, 12, 14)
            self._workspace_layout.setSpacing(10)
            self._content_layout.setContentsMargins(12, 11, 12, 11)
            self._content_layout.setSpacing(8)
            self._brand_title.setText("OF")
            self._brand_caption.hide()
            self._page_caption.hide()
            self._version_label.setText(f"v{__version__}")
            self._search.setPlaceholderText("일정 검색" if self._calendar_active else "업무 검색")
            self._add_button.setText("+ 일정" if self._calendar_active else "+ 업무")
        else:
            self._sidebar.setFixedWidth(212 if show_detail else 180)
            self._sidebar_layout.setContentsMargins(18, 24, 18, 20)
            margin = 24 if show_detail else 18
            self._workspace_layout.setContentsMargins(margin, 18, margin, 20)
            self._workspace_layout.setSpacing(16)
            self._content_layout.setContentsMargins(22, 20, 22, 20)
            self._content_layout.setSpacing(14)
            self._brand_title.setText("OfficeFlow")
            self._brand_caption.show()
            self._page_caption.show()
            self._version_label.setText(f"OfficeFlow {__version__}")
            self._search.setPlaceholderText(
                "캘린더 일정 검색  (Ctrl+K)"
                if self._calendar_active
                else "업무와 내용 검색  (Ctrl+K)"
            )
            self._add_button.setText("+ 새 일정" if self._calendar_active else "+ 새 업무")

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

        self._page_caption.setVisible(not compact_navigation)

    @staticmethod
    def _set_nav_selected(button: QPushButton, selected: bool) -> None:
        button.setProperty("selected", selected)
        button.style().unpolish(button)
        button.style().polish(button)

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
        self._persist_settings()
        if (
            not self._force_quit
            and self._settings.minimize_to_tray
            and self._tray_icon is not None
            and self._tray_icon.isVisible()
        ):
            self.hide()
            event.ignore()
            if not self._tray_hint_shown:
                self._tray_icon.showMessage(
                    "OfficeFlow가 계속 실행 중입니다.",
                    "알림을 놓치지 않도록 시스템 트레이에서 실행됩니다.",
                    QSystemTrayIcon.MessageIcon.Information,
                    5_000,
                )
                self._tray_hint_shown = True
            return
        self.shutdown()
        super().closeEvent(event)

    def _persist_settings(self) -> None:
        if self._save_settings is None:
            return
        self._remember_view_preferences()
        geometry = self.normalGeometry()
        self._settings = replace(
            self._settings,
            window_width=max(self.MINIMUM_WIDTH, geometry.width()),
            window_height=max(self.MINIMUM_HEIGHT, geometry.height()),
            window_x=geometry.x(),
            window_y=geometry.y(),
            compact_list=True,
            collapsed_today_groups=tuple(
                group.value for group in TaskGroup if group in self._collapsed_groups
            ),
            view_preferences={
                view: dict(preferences) for view, preferences in self._view_preferences.items()
            },
        )
        self._save_settings(self._settings)

    @staticmethod
    def _named_label(text: str, object_name: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(object_name)
        return label
