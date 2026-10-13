from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from re import fullmatch
from threading import Event
from typing import cast
from zoneinfo import ZoneInfo

from PySide6.QtCore import (
    QDate,
    QEvent,
    QItemSelectionModel,
    QObject,
    QPoint,
    QRect,
    QSignalBlocker,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.attachment_export import AttachmentExportService, FileReference
from officeflow.application.attachments import (
    AttachmentCanceledError,
    AttachmentIntegrity,
    AttachmentOperationError,
    AttachmentQuickSample,
    AttachmentService,
    AttachmentVerification,
)
from officeflow.application.file_operations import FileInUseError
from officeflow.application.folder_links import FolderLinkService
from officeflow.application.records import DuplicateWorkLogError, RecordService
from officeflow.application.task_mutations import MutationAttempt
from officeflow.application.tasks import TaskService
from officeflow.domain.attachment import Attachment, AttachmentCheckStatus, AttachmentMove
from officeflow.domain.records import ChecklistItem, RecordValidationError, WorkLog
from officeflow.domain.task import Task
from officeflow.infrastructure.database.document_catalog import AttachmentSourceSummary
from officeflow.presentation.attachment_browser import (
    AttachmentDetailLabel,
    AttachmentSourceController,
)
from officeflow.presentation.attachment_export import AttachmentExportController
from officeflow.presentation.attachment_metadata import AttachmentMetadataDialog
from officeflow.presentation.attachment_thumbnails import (
    AttachmentImageCoordinator,
    ThumbnailFileDelegate,
    visible_items,
)
from officeflow.presentation.background import OperationWorker, finish_thread
from officeflow.presentation.compact_labels import CompactLabel, WrappingLabel
from officeflow.presentation.dates import date_with_weekday, format_date, format_datetime
from officeflow.presentation.file_list import compact_file_list, set_file_row
from officeflow.presentation.folder_links import FolderLinksPanel
from officeflow.presentation.image_attachments import (
    ClipboardAttachmentDialog,
    ImagePreviewDialog,
    is_image_name,
)
from officeflow.presentation.lookup_style import SCROLLBAR_STYLE
from officeflow.presentation.theme import LIGHT_STYLESHEET


def _qdate(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


@dataclass(slots=True)
class _RecordGroup:
    key: tuple[str, int]
    task: Task | None = None
    completed: Task | None = None
    occurrence_start: datetime | None = None
    logs: list[WorkLog] = field(default_factory=list)
    match_locations: set[str] = field(default_factory=set)
    work_log_count: int = 0
    summary: str = ""


class AttachmentImportWorker(QObject):
    fileStarted = Signal(int, int, str)
    succeeded = Signal(object)
    failed = Signal(object, object)
    canceled = Signal()
    finished = Signal()

    def __init__(
        self,
        service: AttachmentService,
        task_id: int,
        sources: tuple[Path, ...],
    ) -> None:
        super().__init__()
        self._service = service
        self._task_id = task_id
        self._sources = sources
        self._canceled = Event()

    def cancel(self) -> None:
        self._canceled.set()

    @Slot()
    def run(self) -> None:
        total = len(self._sources)
        for index, source in enumerate(self._sources, start=1):
            if self._canceled.is_set():
                self.canceled.emit()
                break
            self.fileStarted.emit(index, total, source.name)
            try:
                attachment = self._service.attach(
                    self._task_id,
                    source,
                    cancel_requested=self._canceled.is_set,
                )
            except AttachmentCanceledError:
                self.canceled.emit()
                break
            except Exception as error:
                self.failed.emit(source, error)
            else:
                self.succeeded.emit(attachment)
        self.finished.emit()


class AttachmentListWidget(QListWidget):
    filesDropped = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._has_local_files(event.mimeData().urls()):
            self._set_drag_active(True)
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self._has_local_files(event.mimeData().urls()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._set_drag_active(False)
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_drag_active(False)
        sources = tuple(
            Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()
        )
        if not sources:
            event.ignore()
            return
        self.filesDropped.emit(sources)
        event.acceptProposedAction()

    @staticmethod
    def _has_local_files(urls: list[QUrl]) -> bool:
        return any(url.isLocalFile() for url in urls)

    def _set_drag_active(self, active: bool) -> None:
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)


class _FileSamplesWorker(QObject):
    """One owned thread per open record window, not one native thread per click."""

    requested = Signal(object)
    succeeded = Signal(object)
    failed = Signal(object)
    finished = Signal()

    def __init__(self, service: AttachmentService) -> None:
        super().__init__()
        self.service = service
        self._canceled = Event()
        self.requested.connect(self.run, Qt.ConnectionType.QueuedConnection)

    def cancel(self) -> None:
        self._canceled.set()

    def request(self, value: object) -> None:
        self._canceled.clear()
        self.requested.emit(value)

    @Slot(object)
    def run(self, value: object) -> None:
        generation, samples = cast(tuple[int, tuple[Attachment, ...]], value)
        try:
            basic = self.service.quick_samples(samples, cancel_requested=self._canceled.is_set)
            extra = (
                self.service.lifecycle.cache.check_assets(
                    tuple(item.asset_id for item in samples), self._canceled.is_set
                )
                if self.service.lifecycle
                else ()
            )
        except Exception as error:
            self.failed.emit(error)
        else:
            self.succeeded.emit((generation, (*basic, *extra)))
        finally:
            self.finished.emit()


class CompleteTaskDialog(QDialog):
    def __init__(
        self,
        task: Task,
        *,
        result_note: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("업무 완료")
        self.setModal(True)
        self.resize(500, 320)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        title = QLabel(task.title)
        title.setObjectName("pageTitle")
        title.setWordWrap(True)
        root.addWidget(title)
        help_label = QLabel(
            "완료 요약을 바로 남기고 완료할 수 있습니다. 요약 입력은 선택 사항입니다."
        )
        help_label.setObjectName("mutedText")
        help_label.setWordWrap(True)
        root.addWidget(help_label)
        self.result_edit = QTextEdit()
        self.result_edit.setObjectName("completionResultEdit")
        self.result_edit.setPlaceholderText("완료 요약, 결정 사항 또는 인계 내용 (선택)")
        self.result_edit.setPlainText(result_note)
        self.result_edit.setTabChangesFocus(True)
        root.addWidget(self.result_edit, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("완료 저장")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def result_note(self) -> str:
        return self.result_edit.toPlainText().strip()


class TaskRecordsDialog(QDialog):
    changed = Signal()
    tabChanged = Signal(str)
    attachmentMetadataChanged = Signal(int)
    exportDirectoryChanged = Signal(str)
    windowSizeChanged = Signal(int, int)
    TAB_KEYS = ("checklist", "result", "work_log", "attachments", "folders")

    def __init__(
        self,
        task: Task,
        *,
        task_service: TaskService,
        record_service: RecordService,
        attachment_service: AttachmentService | None = None,
        attachment_export_service: AttachmentExportService | None = None,
        attachment_export_directory: str | None = None,
        occurrence_start: datetime | None = None,
        initial_tab: str | None = None,
        folder_link_service: FolderLinkService | None = None,
        image_coordinator: AttachmentImageCoordinator | None = None,
        initial_window_size: tuple[int | None, int | None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setStyleSheet(LIGHT_STYLESHEET)
        if task.id is None:
            raise ValueError("저장된 업무만 기록을 작성할 수 있습니다.")
        self._task = task
        self._task_id = task.id
        self._read_only = task.deleted_at is not None
        self._task_service = task_service
        self._result_attempt = MutationAttempt()
        self._record_service = record_service
        self._attachment_service = attachment_service
        self._attachment_export_service = attachment_export_service
        self._export_controller = (
            AttachmentExportController(
                attachment_export_service, self, initial_directory=attachment_export_directory
            )
            if attachment_export_service
            else None
        )
        if self._export_controller:
            self._export_controller.savedDirectory.connect(self.exportDirectoryChanged)
            self._export_controller.busyChanged.connect(
                lambda _busy: self._attachment_selected(self.attachment_list.currentItem(), None)
            )
        self._occurrence_start = occurrence_start
        self._checklist_by_id: dict[int, ChecklistItem] = {}
        self._work_logs_by_id: dict[int, WorkLog] = {}
        self._selected_log_id: int | None = None
        self._loading_checklist = False
        self._attachments_by_id: dict[int, Attachment] = {}
        self._selected_attachment_id: int | None = None
        self._attachment_kind = "all"
        self._attachments_loaded = False
        self._source_controller: AttachmentSourceController | None = None
        self._source_epoch = 0
        self._source_selection: tuple[int, ...] = ()
        self._source_info_dialog: QDialog | None = None
        self._source_info_attachment: Attachment | None = None
        self._source_info_token: tuple[int, int, int, int] | None = None
        self._size_saved = False
        self._previous_geometry: QRect | None = None
        self._clipboard_dialog: ClipboardAttachmentDialog | None = None
        self._image_preview: ImagePreviewDialog | None = None
        self._image_coordinator = image_coordinator or AttachmentImageCoordinator(self)
        self._owns_image_coordinator = image_coordinator is None
        self._thumbnail_client = self._image_coordinator.register()
        self.attachment_view_mode = self._image_coordinator.default_view
        self._thumbnail_epoch = 0
        self._thumbnail_force_epoch = -1
        self._thumbnail_closed = False
        self._image_action_pending = False
        self._image_action_resuming = False
        self._thumbnail_timer = QTimer(self)
        self._thumbnail_timer.setSingleShot(True)
        self._thumbnail_timer.setInterval(100)
        self._thumbnail_timer.timeout.connect(self._load_visible_thumbnails)
        self._image_coordinator.updated.connect(self._thumbnail_updated)
        self._samples: dict[int, AttachmentQuickSample] = {}
        self._cache_states: dict[str, str] = {}
        self._sample_thread: QThread | None = None
        self._sample_worker: _FileSamplesWorker | None = None
        self._sample_busy = False
        self._sample_generation = 0
        self._sample_timer = QTimer(self)
        self._sample_timer.setSingleShot(True)
        self._sample_timer.setInterval(100)
        self._sample_timer.timeout.connect(self._sample_visible_files)
        self._attachment_thread: QThread | None = None
        self._attachment_worker: AttachmentImportWorker | None = None
        self._verification_thread: QThread | None = None
        self._verification_worker: OperationWorker | None = None
        self._verification_progress: QProgressDialog | None = None
        self._verification_attachment_id: int | None = None
        self._close_when_verification_finishes = False
        self._owner_shutting_down = False
        self._attachment_progress: QProgressDialog | None = None
        self._attachment_import_successes = 0
        self._attachment_import_failures: list[tuple[Path, str]] = []
        self._attachment_import_warnings: list[str] = []
        self._attachment_import_canceled = False
        self._close_when_attachment_finishes = False
        self._attachment_refresh_pending = False
        self._attachment_refresh_timer = QTimer(self)
        self._attachment_refresh_timer.setSingleShot(True)
        self._attachment_refresh_timer.timeout.connect(self._refresh_pending_attachments)
        self.attachmentMetadataChanged.connect(
            self._attachments_changed, Qt.ConnectionType.QueuedConnection
        )

        self.setWindowTitle(f"업무 기록 · {task.title}")
        self.setModal(False)
        self.resize(
            (initial_window_size or (None, None))[0] or 620,
            (initial_window_size or (None, None))[1] or 650,
        )
        self.setMinimumSize(500, 540)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        heading = CompactLabel(task.title)
        heading.setObjectName("pageTitle")
        self._heading = heading
        self._heading_title = task.title
        root.addWidget(heading)
        self._heading_wrapped = QLabel(task.title)
        self._heading_wrapped.setObjectName("pageTitle")
        self._heading_wrapped.setWordWrap(True)
        root.addWidget(self._heading_wrapped)
        context = "이 반복 일정 건의 기록" if occurrence_start is not None else "업무 전체 기록"
        if self._read_only:
            context += " · 휴지통에서는 조회만 가능하며 수정하려면 먼저 복원하세요."
        caption = WrappingLabel()
        caption.setText(context)
        caption.setObjectName("mutedText")
        root.addWidget(caption)

        self.tabs = QTabWidget()
        self.tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.tabs.setObjectName("taskRecordsTabs")
        self.tabs.addTab(self._build_checklist_tab(), "체크리스트")
        self.tabs.addTab(self._build_result_tab(), "완료 요약")
        self.tabs.addTab(self._build_work_log_tab(), "업무일지")
        self._tab_keys = ["checklist", "result", "work_log"]
        if attachment_service is not None:
            self.tabs.addTab(self._build_attachment_tab(), "첨부파일")
            self._tab_keys.append("attachments")
        self.folder_panel: FolderLinksPanel | None = None
        if folder_link_service is not None:
            self.folder_panel = FolderLinksPanel(
                folder_link_service, self._task_id, read_only=self._read_only, parent=self
            )
            self.folder_panel.changed.connect(self.changed)
            self.tabs.addTab(self.folder_panel, "폴더")
            self._tab_keys.append("folders")
        if initial_tab is not None:
            self.select_tab(initial_tab)
        self.tabs.currentChanged.connect(lambda _index: self.tabChanged.emit(self.current_tab_key))
        self.tabs.currentChanged.connect(lambda _index: self._schedule_file_samples())
        self.tabs.currentChanged.connect(lambda _index: self._schedule_thumbnails(recheck=True))
        self.tabs.currentChanged.connect(lambda _index: self._refresh_source_summary())
        self.tabs.currentChanged.connect(lambda _index: self._update_record_heading())
        root.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        history_button = QPushButton("이력·시점 보관")
        history_button.setObjectName("taskHistoryButton")
        history_button.clicked.connect(self._open_history)
        buttons.addButton(history_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._update_record_heading()

        self._refresh_checklist()
        self._expected_result = task_service.result_note(self._task_id, occurrence_start)
        self.result_edit.setPlainText(self._expected_result)
        self._refresh_work_logs()
        if attachment_service is not None:
            self._refresh_attachments()
        if self._read_only:
            self._apply_read_only_mode()
        if attachment_service is not None:
            self._unsubscribe_attachments = attachment_service.subscribe(
                self._relay_attachment_change
            )
            unsubscribe = self._unsubscribe_attachments
            self.finished.connect(lambda _result: unsubscribe())
            self.destroyed.connect(unsubscribe)
        self.finished.connect(lambda _result: self._finish_browser_state())

    def _update_record_heading(self) -> None:
        attachments = self.current_tab_key == "attachments"
        self._heading.setVisible(attachments)
        self._heading_wrapped.setVisible(not attachments)

    def _finish_browser_state(self) -> None:
        if self._source_controller:
            self._source_controller.shutdown()
        if self._size_saved:
            return
        self._size_saved = True
        size = (
            self.normalGeometry().size()
            if self.isMaximized() or self.isMinimized()
            else self.size()
        )
        if 200 <= size.width() <= 8192 and 200 <= size.height() <= 8192:
            self.windowSizeChanged.emit(size.width(), size.height())

    def _fit_browser_geometry(self, geometry: QRect) -> QRect:
        available = self.screen().availableGeometry()
        margins = self.frameGeometry().size() - self.size()
        width = max(200, available.width() - max(0, margins.width()) - 8)
        height = max(200, available.height() - max(0, margins.height()) - 8)
        self.setMinimumSize(min(500, width), min(540, height))
        geometry.setSize(
            QSize(
                min(max(self.minimumWidth(), geometry.width()), width),
                min(max(self.minimumHeight(), geometry.height()), height),
            )
        )
        geometry.moveLeft(
            max(
                available.left() + 4, min(geometry.left(), available.right() - geometry.width() - 4)
            )
        )
        geometry.moveTop(
            max(
                available.top() + 4,
                min(
                    geometry.top(),
                    available.bottom() - geometry.height() - max(0, margins.height()) - 4,
                ),
            )
        )
        return geometry

    def _toggle_wide_attachment_view(self) -> None:
        if self._previous_geometry is not None:
            geometry, self._previous_geometry = self._previous_geometry, None
            self.wide_attachment_button.setText("넓게 보기")
        else:
            self._previous_geometry = QRect(self.geometry())
            geometry = QRect(self.geometry())
            geometry.setSize(QSize(max(self.width(), 940), max(self.height(), 720)))
            self.wide_attachment_button.setText("이전 크기")
        self.setGeometry(self._fit_browser_geometry(geometry))

    def _visible_attachments(self) -> tuple[Attachment, ...]:
        return tuple(
            item
            for item in self._attachments_by_id.values()
            if self._attachment_kind == "all" or item.role.value == self._attachment_kind
        )

    def _selected_attachment_items(self) -> tuple[QListWidgetItem, ...]:
        visible = {item.id for item in self._visible_attachments()}
        return tuple(
            item
            for item in self.attachment_list.selectedItems()
            if not item.isHidden() and item.data(Qt.ItemDataRole.UserRole) in visible
        )

    def _selected_attachment_ids(self) -> tuple[int, ...]:
        return tuple(
            int(item.data(Qt.ItemDataRole.UserRole)) for item in self._selected_attachment_items()
        )

    def _attachment_filter_busy(self) -> bool:
        return bool(
            self._image_action_pending
            or self._attachment_thread
            or self._verification_thread
            or (self._export_controller and self._export_controller.busy)
            or getattr(self.property("documentActionsController"), "_job_thread", None)
        )

    def _change_attachment_kind(self) -> None:
        kind = str(self.attachment_kind_combo.currentData())
        if self._attachment_filter_busy():
            blocker = QSignalBlocker(self.attachment_kind_combo)
            self.attachment_kind_combo.setCurrentIndex(
                self.attachment_kind_combo.findData(self._attachment_kind)
            )
            del blocker
            return
        self._attachment_kind = kind
        self._refresh_attachments()

    def _clear_attachment_kind(self) -> None:
        self.attachment_kind_combo.setCurrentIndex(0)

    def _invalidate_source_summary(self) -> None:
        self._source_epoch += 1
        self._source_selection = ()
        if self._source_controller:
            self._source_controller.invalidate()

    def _refresh_source_summary(self, *, force: bool = False) -> None:
        if self._owner_shutting_down or not hasattr(self, "attachment_list"):
            return
        ids = self._selected_attachment_ids()
        if ids != self._source_selection:
            self._invalidate_source_summary()
            self._source_selection = ids
        if len(ids) != 1:
            return
        item = self._attachments_by_id[ids[0]]
        service = self._attachment_service
        if (
            service is None
            or service.lifecycle is None
            or (not force and (not self.isVisible() or self.current_tab_key != "attachments"))
        ):
            return
        if self._source_controller is None:
            self._source_controller = AttachmentSourceController(
                service.lifecycle.catalog.source_summary, self
            )
            self._source_controller.updated.connect(self._source_summary_ready)
            self._source_controller.informationReady.connect(self._source_information_ready)
        self._source_controller.request(
            self._task_id, ids[0], item.metadata_revision, self._source_epoch
        )

    def _source_summary_ready(self) -> None:
        if self._owner_shutting_down or len(self._selected_attachment_ids()) != 1:
            return
        if self.attachment_detail.is_summary:
            self._render_attachment_summary()

    @Slot(object)
    def _source_information_ready(self, value: object) -> None:
        from shiboken6 import isValid

        token, source = cast(tuple[tuple[int, int, int, int], AttachmentSourceSummary], value)
        dialog, item = self._source_info_dialog, self._source_info_attachment
        if (
            self._owner_shutting_down
            or token != self._source_info_token
            or dialog is None
            or item is None
            or not isValid(dialog)
            or not dialog.isVisible()
        ):
            return
        edit = dialog.findChild(QPlainTextEdit)
        if edit is not None:
            edit.setPlainText(self._attachment_information(item) + "\n" + source.information)

    def _render_attachment_summary(self) -> None:
        item = self._attachments_by_id.get(self._selected_attachment_id or -1)
        if item is None:
            return
        if item.role.value == "work":
            source = self._source_controller.result if self._source_controller else None
            waiting = (
                "출처 확인 중"
                if self._attachment_service and self._attachment_service.lifecycle
                else "출처 정보 없음"
            )
            text = f"작업본 · {source.text if source else waiting}"
        elif item.is_frozen:
            text = "원본 · 내용 고정"
        else:
            text = f"일반 첨부 · {self._format_size(item.display_size_bytes)}"
        self.attachment_detail.set_summary(text)

    def _relay_attachment_change(self, task_id: int) -> None:
        self.attachmentMetadataChanged.emit(task_id)

    def _open_history(self) -> None:
        from shiboken6 import isValid

        from officeflow.presentation.task_history_dialog import TaskHistoryDialog

        existing = getattr(self, "_history_dialog", None)
        if existing is not None and isValid(existing) and not existing._closed:
            existing.show()
            existing.raise_()
            return
        dialog = TaskHistoryDialog(
            self._task_id,
            self._task.title,
            self._task_service.history,
            occurrence_start=self._occurrence_start,
            timezone=self._task.timezone,
            read_only=self._read_only,
            lifecycle=self._attachment_service.lifecycle if self._attachment_service else None,
            export_service=self._attachment_export_service,
            draft_guard=self._snapshot_draft_guard,
            parent=self,
        )
        self._history_dialog = dialog
        self.finished.connect(lambda _code: dialog.shutdown())
        dialog.show()

    def _snapshot_draft_guard(self) -> bool:
        if self.result_edit.toPlainText().strip() == self._expected_result:
            return True
        answer = QMessageBox.question(
            self,
            "미저장 완료 요약",
            "완료 요약에 미저장 내용이 있습니다. DB에 저장된 내용만 보관할까요?\n취소하면 돌아가 저장할 수 있습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    @Slot(int)
    def _attachments_changed(self, task_id: int) -> None:
        if task_id != self._task_id or self._owner_shutting_down:
            return
        self._attachment_refresh_pending = True
        if self._attachment_thread is None and self._verification_thread is None:
            self._attachment_refresh_timer.start(0)

    def _refresh_pending_attachments(self) -> None:
        if not self._attachment_refresh_pending or self._owner_shutting_down:
            return
        if self._attachment_thread is not None or self._verification_thread is not None:
            return
        self._attachment_refresh_pending = False
        try:
            self._refresh_attachments(metadata_only=True)
        except Exception as error:
            self.attachment_detail.setText(str(error))
            self._set_attachment_actions_enabled(False)

    def _apply_read_only_mode(self) -> None:
        self.checklist_edit.setReadOnly(True)
        self.checklist_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for index in range(self.checklist_list.count()):
            item = self.checklist_list.item(index)
            item.setFlags(
                item.flags() & ~Qt.ItemFlag.ItemIsEditable & ~Qt.ItemFlag.ItemIsUserCheckable
            )
        self.result_edit.setReadOnly(True)
        self.log_date_edit.setEnabled(False)
        self.log_content_edit.setReadOnly(True)
        self.log_result_edit.setReadOnly(True)
        for tab_index in range(min(3, self.tabs.count())):
            tab = self.tabs.widget(tab_index)
            if tab is None:
                continue
            for button in tab.findChildren(QPushButton):
                button.setEnabled(False)
        if self._attachment_service is not None:
            self.add_attachment_button.setEnabled(False)
            self.attachment_list.setAcceptDrops(False)
            self.attachment_drop_hint.setText("휴지통의 업무는 첨부파일을 열어볼 수만 있습니다.")
            self.unlink_attachment_button.setEnabled(False)
            self.delete_attachment_button.setEnabled(False)

    def _build_checklist_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 14, 10, 10)
        add_row = QHBoxLayout()
        self.checklist_edit = QLineEdit()
        self.checklist_edit.setObjectName("checklistInput")
        self.checklist_edit.setPlaceholderText("할 일을 입력하고 Enter")
        self.checklist_edit.returnPressed.connect(self._add_checklist)
        add_row.addWidget(self.checklist_edit, 1)
        add_button = QPushButton("추가")
        add_button.setObjectName("addChecklistButton")
        add_button.clicked.connect(self._add_checklist)
        add_row.addWidget(add_button)
        layout.addLayout(add_row)

        self.checklist_list = QListWidget()
        self.checklist_list.setObjectName("checklistList")
        self.checklist_list.setAlternatingRowColors(True)
        self.checklist_list.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.checklist_list.itemChanged.connect(self._checklist_changed)
        self.checklist_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.checklist_list.customContextMenuRequested.connect(self._show_checklist_menu)
        layout.addWidget(self.checklist_list, 1)

        actions = QHBoxLayout()
        delete_button = QPushButton("선택 삭제")
        delete_button.setObjectName("deleteChecklistButton")
        delete_button.clicked.connect(self._delete_checklist)
        actions.addStretch()
        actions.addWidget(delete_button)
        layout.addLayout(actions)
        return tab

    def _build_checklist_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setStyleSheet(LIGHT_STYLESHEET)
        row = self.checklist_list.currentRow()
        editable = row >= 0 and not self._read_only
        edit = menu.addAction("수정")
        edit.setEnabled(editable)
        edit.triggered.connect(self._edit_checklist)
        menu.addSeparator()
        for label, direction in (("위로 이동", -1), ("아래로 이동", 1)):
            action = menu.addAction(label)
            action.setEnabled(editable and 0 <= row + direction < self.checklist_list.count())
            action.triggered.connect(
                lambda _checked=False, step=direction: self._move_checklist(step)
            )
        menu.addSeparator()
        delete = menu.addAction("삭제")
        delete.setEnabled(editable)
        delete.triggered.connect(self._delete_checklist)
        return menu

    def _edit_checklist(self) -> None:
        item = self.checklist_list.currentItem()
        if item is not None and not self._read_only:
            self.checklist_list.editItem(item)

    def _show_checklist_menu(self, position: QPoint) -> None:
        item = self.checklist_list.itemAt(position)
        if item is None:
            return
        self.checklist_list.setCurrentItem(item)
        menu = self._build_checklist_menu()
        menu.exec(self.checklist_list.viewport().mapToGlobal(position))
        menu.deleteLater()

    def _selected_attachment_family(self) -> str | None:
        ids = self._selected_attachment_ids()
        selected = self._attachments_by_id.get(ids[0]) if len(ids) == 1 else None
        family = selected.family_id if selected else ""
        return family if fullmatch(r"[0-9a-f]{32}", family) else None

    def _open_document_browser(self, *, initial_kind: str = "version") -> None:
        if (
            not self._attachment_service
            or not self._attachment_service.lifecycle
            or not self._export_controller
        ):
            return
        from officeflow.presentation.document_actions import DocumentBrowser, file_actions

        dialog = DocumentBrowser(
            self._attachment_service.lifecycle,
            self._export_controller._service,
            self._task_id,
            family_id=self._selected_attachment_family(),
            initial_kind=initial_kind,
            read_only=self._read_only,
            timezone=self._task.timezone,
            parent=self,
        )
        file_actions(self, self._attachment_service.lifecycle).keep(dialog)

    def _open_related_files(self) -> None:
        if self._selected_attachment_family() is not None:
            self._open_document_browser(initial_kind="attachment")

    def _create_document(self, operation: str) -> None:
        if self._defer_image_action(lambda: self._create_document(operation)):
            return
        if (
            not self._attachment_metadata_enabled()
            or not self._attachment_service
            or not self._attachment_service.lifecycle
            or self._selected_attachment_id is None
        ):
            return
        from officeflow.application.attachment_documents import DocumentOperation
        from officeflow.presentation.document_actions import file_actions

        service = self._attachment_service.lifecycle
        entry = service.catalog.get("attachment", self._selected_attachment_id)
        file_actions(self, service).create(
            entry,
            DocumentOperation(operation),
            self._document_changed,
        )

    def _open_view_copies(self) -> None:
        if not self._attachment_service or not self._attachment_service.lifecycle:
            return
        from officeflow.presentation.document_actions import ViewCacheDialog, file_actions

        selected = self._attachments_by_id.get(self._selected_attachment_id or -1)
        lifecycle = self._attachment_service.lifecycle
        dialog = ViewCacheDialog(
            lifecycle,
            self,
            exports=self._export_controller._service if self._export_controller else None,
            asset_id=selected.asset_id if selected else None,
        )
        file_actions(self, lifecycle).keep(dialog)

    def _document_changed(self) -> None:
        self._attachments_changed(self._task_id)
        self._refresh_attachments()
        self.changed.emit()

    def _build_result_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 14, 10, 10)
        help_label = QLabel("업무를 마친 뒤 남길 핵심 결과와 결정 사항을 요약하세요.")
        help_label.setObjectName("mutedText")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        self.result_edit = QTextEdit()
        self.result_edit.setObjectName("resultNoteEdit")
        self.result_edit.setPlaceholderText("완료 요약")
        self.result_edit.setTabChangesFocus(True)
        layout.addWidget(self.result_edit, 1)
        save_button = QPushButton("완료 요약 저장")
        save_button.setObjectName("saveResultButton")
        save_button.setProperty("primaryAction", True)
        save_button.clicked.connect(self._save_result)
        layout.addWidget(save_button, alignment=Qt.AlignmentFlag.AlignRight)
        return tab

    def _build_work_log_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 14, 10, 10)
        form = QFormLayout()
        self.log_date_edit = QDateEdit(QDate.currentDate())
        self.log_date_edit.setObjectName("workLogDate")
        self.log_date_edit.setCalendarPopup(True)
        self.log_date_edit.setDisplayFormat("yyyy-MM-dd")
        form.addRow("날짜", date_with_weekday(self.log_date_edit))
        self.log_content_edit = QTextEdit()
        self.log_content_edit.setObjectName("workLogContent")
        self.log_content_edit.setPlaceholderText("오늘 진행한 내용을 입력하세요")
        self.log_content_edit.setMaximumHeight(90)
        self.log_content_edit.setTabChangesFocus(True)
        form.addRow("진행 내용 *", self.log_content_edit)
        self.log_result_edit = QTextEdit()
        self.log_result_edit.setObjectName("workLogResult")
        self.log_result_edit.setPlaceholderText("진행 결과, 이슈 또는 다음 단계")
        self.log_result_edit.setMaximumHeight(75)
        self.log_result_edit.setTabChangesFocus(True)
        form.addRow("진행 결과 / 다음 단계", self.log_result_edit)
        layout.addLayout(form)

        edit_actions = QHBoxLayout()
        new_button = QPushButton("새 기록")
        new_button.clicked.connect(self._clear_work_log_form)
        self.save_log_button = QPushButton("기록 추가")
        self.save_log_button.setObjectName("saveWorkLogButton")
        self.save_log_button.setProperty("primaryAction", True)
        self.save_log_button.clicked.connect(self._save_work_log)
        edit_actions.addWidget(new_button)
        edit_actions.addStretch()
        edit_actions.addWidget(self.save_log_button)
        layout.addLayout(edit_actions)

        self.work_log_list = QListWidget()
        self.work_log_list.setObjectName("taskWorkLogList")
        self.work_log_list.currentItemChanged.connect(self._work_log_selected)
        layout.addWidget(self.work_log_list, 1)
        delete_button = QPushButton("선택 기록 삭제")
        delete_button.setObjectName("deleteWorkLogButton")
        delete_button.clicked.connect(self._delete_work_log)
        layout.addWidget(delete_button, alignment=Qt.AlignmentFlag.AlignRight)
        return tab

    def _build_attachment_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        self.attachment_scroll = QScrollArea()
        self.attachment_scroll.setObjectName("attachmentBodyScroll")
        self.attachment_scroll.setWidgetResizable(True)
        self.attachment_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.attachment_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.attachment_scroll.setStyleSheet(
            SCROLLBAR_STYLE
            + "QScrollArea, QScrollArea > QWidget {color:#172033; background:#FFFFFF;}"
        )
        body = QWidget()
        body.setStyleSheet("color:#172033; background:#FFFFFF;")
        self.attachment_scroll.setWidget(body)
        outer.addWidget(self.attachment_scroll)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        help_text = "일반·작업본은 편집용, 원본은 지정한 내용을 유지합니다." + (
            " 첨부와 순서는 이 업무의 모든 반복 회차에서 공유합니다."
            if self._task.recurrence_rule
            else ""
        )
        self.attachment_drop_hint = QLabel(
            "파일을 아래 목록으로 끌어놓거나 여러 파일을 한번에 선택하세요."
        )
        self.attachment_drop_hint.setObjectName("mutedText")
        self.attachment_drop_hint.setToolTip(help_text)
        self.attachment_drop_hint.setWordWrap(True)
        self.attachment_drop_hint.setParent(tab)
        self.attachment_drop_hint.hide()
        header = QHBoxLayout()
        self.attachment_kind_combo = QComboBox()
        self.attachment_kind_combo.setObjectName("attachmentKindFilter")
        for text, value in (
            ("전체 파일", "all"),
            ("원본", "original"),
            ("작업본", "work"),
            ("일반", "general"),
        ):
            self.attachment_kind_combo.addItem(text, value)
        self.attachment_kind_combo.currentIndexChanged.connect(self._change_attachment_kind)
        header.addWidget(self.attachment_kind_combo)
        self.attachment_filter_status = CompactLabel()
        self.clear_attachment_filter = QPushButton("전체 보기")
        self.clear_attachment_filter.clicked.connect(self._clear_attachment_kind)
        self.clear_attachment_filter.hide()
        header.addWidget(self.clear_attachment_filter)
        header.addStretch()
        self.list_view_button = QPushButton("목록")
        self.thumbnail_view_button = QPushButton("썸네일")
        for button in (self.list_view_button, self.thumbnail_view_button):
            button.setCheckable(True)
            button.setAutoDefault(False)
            header.addWidget(button)
        self.list_view_button.clicked.connect(lambda: self._set_attachment_view("list"))
        self.thumbnail_view_button.clicked.connect(lambda: self._set_attachment_view("thumbnails"))

        self.add_attachment_button = QPushButton("파일 추가")
        self.add_attachment_button.setToolTip(
            "파일 선택 창에서 여러 파일을 함께 선택할 수 있습니다."
        )
        self.add_attachment_button.setObjectName("addAttachmentButton")
        self.add_attachment_button.setProperty("primaryAction", True)
        self.add_attachment_button.clicked.connect(self._choose_attachment)
        add_actions = QHBoxLayout()
        add_actions.addWidget(self.add_attachment_button)
        self.clipboard_image_button = QPushButton("클립보드 이미지")
        self.clipboard_image_button.setToolTip("캡처한 사진을 이름·미리보기 확인 후 첨부")
        self.clipboard_image_button.clicked.connect(self._add_clipboard_image)
        self.clipboard_image_button.setEnabled(not self._read_only)
        add_actions.addWidget(self.clipboard_image_button)
        add_actions.addStretch()
        self.wide_attachment_button = QPushButton("넓게 보기")
        self.wide_attachment_button.setObjectName("wideAttachmentView")
        self.wide_attachment_button.clicked.connect(self._toggle_wide_attachment_view)
        add_actions.addWidget(self.wide_attachment_button)
        layout.addLayout(add_actions)
        layout.addLayout(header)
        self.attachment_filter_status.hide()
        layout.addWidget(self.attachment_filter_status)

        self.attachment_list = AttachmentListWidget()
        compact_file_list(self.attachment_list)
        list_minimum = 5 * max(34, self.attachment_list.fontMetrics().height() + 16) + 16
        self.attachment_list.setMinimumHeight(list_minimum)
        self.attachment_list.setObjectName("attachmentList")
        self.attachment_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.attachment_list.viewport().installEventFilter(self)
        self.attachment_list.currentItemChanged.connect(self._attachment_selected)
        self.attachment_list.itemSelectionChanged.connect(
            lambda: self._attachment_selected(self.attachment_list.currentItem(), None)
        )
        self.attachment_list.itemSelectionChanged.connect(self._schedule_file_samples)
        self.attachment_list.verticalScrollBar().valueChanged.connect(
            lambda _value: self._schedule_file_samples()
        )
        self.attachment_list.verticalScrollBar().valueChanged.connect(
            lambda _value: self._schedule_thumbnails()
        )
        self.attachment_list.itemDoubleClicked.connect(self._open_clicked_attachment)
        self.attachment_list.filesDropped.connect(self._add_attachment_sources)
        self.attachment_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.attachment_list.customContextMenuRequested.connect(self._attachment_context_menu)
        self.attachment_stack = QStackedWidget()
        self.attachment_stack.setMinimumHeight(list_minimum)
        self.attachment_stack.addWidget(self.attachment_list)
        self.attachment_empty_label = QLabel()
        self.attachment_empty_label.setWordWrap(True)
        self.attachment_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.attachment_empty_label.setStyleSheet("color:#172033; background:#FFFFFF;")
        self.attachment_stack.addWidget(self.attachment_empty_label)
        layout.addWidget(self.attachment_stack, 1)
        self.attachment_warning = WrappingLabel()
        self.attachment_warning.setStyleSheet("color:#9A3412; background:#FFF7ED;")
        self.attachment_warning.hide()
        layout.addWidget(self.attachment_warning)

        self.attachment_detail = AttachmentDetailLabel()
        self.attachment_detail.set_summary("파일을 선택하세요.")
        self.attachment_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.attachment_detail.setObjectName("attachmentDetail")
        self.attachment_detail.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.attachment_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        summary = QHBoxLayout()
        summary.addWidget(self.attachment_detail, 1)
        self.image_preview_button = QPushButton("미리보기")
        self.image_preview_button.setEnabled(False)
        self.image_preview_button.clicked.connect(self._show_image_preview)
        # Kept as an internal command for compatibility, not a visible per-file button.
        self.image_preview_button.setParent(self)
        self.image_preview_button.hide()
        self.preserve_view_button = QPushButton("수정본 저장…")
        self.preserve_view_button.setObjectName("preserveViewButton")
        self.preserve_view_button.clicked.connect(self._open_view_copies)
        self.preserve_view_button.hide()
        summary.addWidget(self.preserve_view_button)
        layout.addLayout(summary)

        actions = QHBoxLayout()
        self.open_attachment_button = QPushButton("열기")
        self.open_attachment_button.setObjectName("openAttachmentButton")
        self.open_attachment_button.clicked.connect(self._open_attachment)
        self.attachment_info_button = QPushButton("정보")
        self.attachment_info_button.setObjectName("attachmentInfoButton")
        self.attachment_info_button.clicked.connect(self._show_attachment_information)
        self.attachment_info_button.setEnabled(False)
        self.verify_attachment_button = QPushButton("파일 정보 확인", self)
        self.verify_attachment_button.setObjectName("verifyAttachmentButton")
        self.verify_attachment_button.clicked.connect(self._verify_attachment)
        self.unlink_attachment_button = QPushButton("연결 해제", self)
        self.unlink_attachment_button.setObjectName("unlinkAttachmentButton")
        self.unlink_attachment_button.clicked.connect(self._unlink_attachment)
        self.delete_attachment_button = QPushButton("파일 삭제", self)
        self.delete_attachment_button.setObjectName("deleteAttachmentButton")
        self.delete_attachment_button.clicked.connect(self._delete_attachment)
        for button in (
            self.open_attachment_button,
            self.verify_attachment_button,
            self.unlink_attachment_button,
            self.delete_attachment_button,
        ):
            button.setEnabled(False)
        for button in (
            self.verify_attachment_button,
            self.unlink_attachment_button,
            self.delete_attachment_button,
        ):
            button.hide()
        self.save_attachment_button = QPushButton("다른 이름으로 저장…")
        self.save_attachment_button.setObjectName("saveAttachmentCopyButton")
        self.save_attachment_button.clicked.connect(self._save_attachment_copy)
        self.save_attachment_button.setEnabled(False)
        self.more_attachment_button = QPushButton("더보기")
        self.more_attachment_button.setObjectName("moreAttachmentButton")
        more = QMenu(self.more_attachment_button)
        self.attachment_information_action = more.addAction(
            "파일 정보…", self._show_attachment_information
        )
        self.image_expand_action = more.addAction("이미지 확대…", self._show_image_preview)
        more.addAction("썸네일 새로고침", self._refresh_thumbnails)
        more.addSeparator()
        metadata_actions = (
            more.addAction("이름 변경…", lambda: self._edit_attachment_metadata()),
            more.addAction("메모…", lambda: self._edit_attachment_metadata(edit_memo=True)),
        )
        more.addSeparator()
        file_actions = []
        for button in (
            self.verify_attachment_button,
            self.unlink_attachment_button,
            self.delete_attachment_button,
        ):
            action = more.addAction(button.text(), button.click)
            action.setObjectName(button.objectName() + "Action")
            file_actions.append(action)

        def update_more() -> None:
            self.attachment_information_action.setEnabled(self.attachment_info_button.isEnabled())
            self.image_expand_action.setEnabled(self.image_preview_button.isEnabled())
            for action in metadata_actions:
                action.setEnabled(self._attachment_metadata_enabled())
            for action, button in zip(
                file_actions,
                (
                    self.verify_attachment_button,
                    self.unlink_attachment_button,
                    self.delete_attachment_button,
                ),
                strict=True,
            ):
                action.setText(button.text())
                action.setEnabled(button.isEnabled())

        more.aboutToShow.connect(update_more)
        self.more_attachment_button.setMenu(more)
        documents: QPushButton | None = None
        if self._attachment_service and self._attachment_service.lifecycle:
            documents = QPushButton("보관함")
            self.document_browser_button = documents
            documents.setObjectName("documentVersionsButton")
            documents.setToolTip("보관본 조회 · 현재 첨부와 함께 여러 파일 저장")
            documents.clicked.connect(self._open_document_browser)
            more.addSeparator()
            self.related_files_action = more.addAction("관련 파일 보기…", self._open_related_files)
            more.aboutToShow.connect(
                lambda: self.related_files_action.setEnabled(
                    self._selected_attachment_family() is not None
                )
            )
            from officeflow.presentation.document_actions import file_actions as document_actions

            document_actions(self, self._attachment_service.lifecycle).busyChanged.connect(
                lambda _busy: self._attachment_selected(self.attachment_list.currentItem(), None)
            )
            more.addAction("수정본 확인/저장…", self._open_view_copies)
            for label, operation in (
                ("새 작업본…", "create_work"),
                ("보관본 남기기…", "create_version"),
                ("원본으로 지정…", "designate_original"),
                ("원본 지정 해제…", "release_original"),
            ):

                def create_action(_checked: bool = False, value: str = operation) -> None:
                    self._create_document(value)

                action = more.addAction(label, create_action)

                def enable_action(a: object = action, value: str = operation) -> None:
                    if hasattr(a, "setEnabled"):
                        a.setEnabled(self._document_action_enabled(value))

                more.aboutToShow.connect(enable_action)
        for action_button in (
            self.open_attachment_button,
            self.save_attachment_button,
            documents,
            self.more_attachment_button,
        ):
            if action_button is not None:
                actions.addWidget(action_button)
        layout.addLayout(actions)
        self.attachment_info_button.setParent(tab)
        self.attachment_info_button.hide()
        focus_widgets = [
            self.add_attachment_button,
            self.clipboard_image_button,
            self.wide_attachment_button,
            self.attachment_kind_combo,
            self.clear_attachment_filter,
            self.list_view_button,
            self.thumbnail_view_button,
            self.attachment_list,
            self.open_attachment_button,
            self.save_attachment_button,
        ]
        if documents is not None:
            focus_widgets.append(documents)
        focus_widgets.append(self.more_attachment_button)
        for first, second in pairwise(focus_widgets):
            QWidget.setTabOrder(first, second)
        return tab

    def _refresh_checklist(self) -> None:
        items = self._record_service.checklist_for_task(self._task_id)
        self._loading_checklist = True
        self.checklist_list.clear()
        self._checklist_by_id = {item.id: item for item in items if item.id is not None}
        for checklist in items:
            item = QListWidgetItem(checklist.content)
            flags = item.flags()
            if not self._read_only:
                flags |= Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEditable
            else:
                flags &= ~Qt.ItemFlag.ItemIsUserCheckable & ~Qt.ItemFlag.ItemIsEditable
            item.setFlags(flags)
            item.setCheckState(
                Qt.CheckState.Checked if checklist.is_done else Qt.CheckState.Unchecked
            )
            item.setData(Qt.ItemDataRole.UserRole, checklist.id)
            self.checklist_list.addItem(item)
        self._loading_checklist = False

    def _add_checklist(self) -> None:
        if self._read_only:
            return
        try:
            self._record_service.add_checklist_item(self._task_id, self.checklist_edit.text())
        except (RecordValidationError, LookupError) as error:
            QMessageBox.warning(self, "체크리스트를 추가하지 못했습니다.", str(error))
            return
        self.checklist_edit.clear()
        self._refresh_checklist()
        self.changed.emit()

    def _checklist_changed(self, item: QListWidgetItem) -> None:
        if self._loading_checklist or self._read_only:
            return
        item_id = item.data(Qt.ItemDataRole.UserRole)
        checklist = self._checklist_by_id.get(item_id)
        if checklist is None:
            return
        try:
            updated = self._record_service.edit_checklist_item(checklist, item.text())
            updated = self._record_service.set_checklist_done(
                updated,
                item.checkState() == Qt.CheckState.Checked,
            )
        except (RecordValidationError, LookupError) as error:
            QMessageBox.warning(self, "체크리스트를 수정하지 못했습니다.", str(error))
            self._refresh_checklist()
            return
        if updated.id is not None:
            self._checklist_by_id[updated.id] = updated
        self.changed.emit()

    def _delete_checklist(self) -> None:
        if self._read_only:
            return
        item = self.checklist_list.currentItem()
        if item is None:
            return
        item_id = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(item_id, int):
            return
        try:
            self._record_service.delete_checklist_item(item_id)
        except (RecordValidationError, LookupError) as error:
            QMessageBox.warning(self, "체크리스트를 삭제하지 못했습니다.", str(error))
            self._refresh_checklist()
            return
        self._refresh_checklist()
        self.changed.emit()

    def _move_checklist(self, direction: int) -> None:
        if self._read_only:
            return
        row = self.checklist_list.currentRow()
        target = row + direction
        if row < 0 or target < 0 or target >= self.checklist_list.count():
            return
        item_ids = [
            self.checklist_list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.checklist_list.count())
        ]
        item_ids[row], item_ids[target] = item_ids[target], item_ids[row]
        try:
            self._record_service.reorder_checklist(
                self._task_id, tuple(item_id for item_id in item_ids if isinstance(item_id, int))
            )
        except Exception as error:
            QMessageBox.warning(self, "체크리스트 순서를 바꾸지 못했습니다.", str(error))
            return
        self._refresh_checklist()
        self.checklist_list.setCurrentRow(target)
        self.checklist_list.scrollToItem(self.checklist_list.item(target))
        self.changed.emit()

    def _save_result(self) -> None:
        note = self.result_edit.toPlainText()
        identity = self._result_attempt.identify(
            (self._task_id, self._occurrence_start, note, self._expected_result)
        )
        try:
            saved_note = self._task_service.update_result_note(
                self._task_id,
                note,
                occurrence_start=self._occurrence_start,
                expected_note=self._expected_result,
                command_id=identity,
            )
        except Exception as error:
            QMessageBox.warning(self, "완료 요약을 저장하지 못했습니다.", str(error))
            return
        self._expected_result = saved_note
        self._result_attempt.succeeded()
        replayed = bool(getattr(saved_note, "command_replayed", False))
        if replayed:
            self.result_edit.setPlainText(saved_note)
        self.changed.emit()
        QMessageBox.information(
            self,
            "저장 완료",
            "이미 처리된 요청입니다. 현재 완료 요약을 표시했습니다."
            if replayed
            else "완료 요약을 저장했습니다.",
        )

    def _refresh_work_logs(self) -> None:
        logs = self._record_service.work_logs(
            task_id=self._task_id,
            occurrence_start=self._occurrence_start,
            occurrence_only=self._occurrence_start is not None,
        )
        self._work_logs_by_id = {log.id: log for log in logs if log.id is not None}
        self.work_log_list.clear()
        for log in logs:
            preview = " ".join(log.content.splitlines())
            item = QListWidgetItem(f"{format_date(log.log_date)}  {preview}")
            item.setData(Qt.ItemDataRole.UserRole, log.id)
            self.work_log_list.addItem(item)

    def _work_log_selected(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        if current is None:
            return
        log_id = current.data(Qt.ItemDataRole.UserRole)
        work_log = self._work_logs_by_id.get(log_id)
        if work_log is None:
            return
        self._selected_log_id = work_log.id
        self.log_date_edit.setDate(_qdate(work_log.log_date))
        self.log_content_edit.setPlainText(work_log.content)
        self.log_result_edit.setPlainText(work_log.result)
        self.save_log_button.setText("기록 수정")

    def _clear_work_log_form(self) -> None:
        self._selected_log_id = None
        self.work_log_list.clearSelection()
        self.log_date_edit.setDate(QDate.currentDate())
        self.log_content_edit.clear()
        self.log_result_edit.clear()
        self.save_log_button.setText("기록 추가")
        self.log_content_edit.setFocus()

    def _save_work_log(
        self,
        _checked: bool = False,
        *,
        allow_duplicate: bool = False,
    ) -> None:
        try:
            if self._selected_log_id is None:
                self._record_service.add_work_log(
                    task_id=self._task_id,
                    occurrence_start=self._occurrence_start,
                    log_date=cast(date, self.log_date_edit.date().toPython()),
                    content=self.log_content_edit.toPlainText(),
                    result=self.log_result_edit.toPlainText(),
                    allow_duplicate=allow_duplicate,
                )
            else:
                self._record_service.update_work_log(
                    self._selected_log_id,
                    log_date=cast(date, self.log_date_edit.date().toPython()),
                    content=self.log_content_edit.toPlainText(),
                    result=self.log_result_edit.toPlainText(),
                    allow_duplicate=allow_duplicate,
                )
        except DuplicateWorkLogError as error:
            answer = QMessageBox.question(
                self,
                "중복 업무일지",
                f"{error}\n그래도 새 기록으로 저장하시겠습니까?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._save_work_log(allow_duplicate=True)
            return
        except (RecordValidationError, LookupError) as error:
            QMessageBox.warning(self, "업무일지를 저장하지 못했습니다.", str(error))
            return
        self._clear_work_log_form()
        self._refresh_work_logs()
        self.changed.emit()

    def _delete_work_log(self) -> None:
        if self._selected_log_id is None:
            return
        self._record_service.delete_work_log(self._selected_log_id)
        self._clear_work_log_form()
        self._refresh_work_logs()
        self.changed.emit()

    @property
    def current_tab_key(self) -> str:
        return self._tab_keys[self.tabs.currentIndex()]

    def select_tab(self, key: str) -> None:
        if key in self._tab_keys:
            index = self._tab_keys.index(key)
            if index < self.tabs.count():
                self.tabs.setCurrentIndex(index)

    def _refresh_attachments(self, *, metadata_only: bool = False) -> None:
        if self._attachment_service is None:
            return
        attachments = self._attachment_service.attachment_metadata_for_task(self._task_id)
        self._document_counts = (
            self._attachment_service.lifecycle.catalog.counts(self._task_id)
            if self._attachment_service.lifecycle
            else {}
        )
        if metadata_only and attachments == tuple(self._attachments_by_id.values()):
            detail = self.attachment_detail.full_text
            summary = self.attachment_detail.is_summary
            self._invalidate_source_summary()
            self._attachment_selected(self.attachment_list.currentItem(), None)
            if not summary:
                self.attachment_detail.setText(detail)
            return
        selected_id = self._selected_attachment_id
        selected_ids = {
            item.data(Qt.ItemDataRole.UserRole) for item in self._selected_attachment_items()
        }
        anchor = self._attachment_scroll_anchor()
        detail = self.attachment_detail.full_text
        was_summary = self.attachment_detail.is_summary
        previous_selected = (
            self._attachments_by_id.get(selected_id) if selected_id is not None else None
        )
        old_ids = set(self._attachments_by_id)
        self._invalidate_source_summary()
        self._attachments_by_id = {
            attachment.id: attachment for attachment in attachments if attachment.id is not None
        }
        added_ids = set(self._attachments_by_id) - old_ids if self._attachments_loaded else set()
        self._attachments_loaded = True
        if added_ids:
            self._attachment_kind = "all"
        blocker = QSignalBlocker(self.attachment_kind_combo)
        for index, value in enumerate(("all", "original", "work", "general")):
            count = sum(
                value == "all" or attachment.role.value == value for attachment in attachments
            )
            label = {"all": "전체 파일", "original": "원본", "work": "작업본", "general": "일반"}[
                value
            ]
            self.attachment_kind_combo.setItemText(index, f"{label} ({count})")
        self.attachment_kind_combo.setCurrentIndex(
            self.attachment_kind_combo.findData(self._attachment_kind)
        )
        del blocker
        visible = self._visible_attachments()
        self.attachment_list.blockSignals(True)
        self.attachment_list.clear()
        for attachment in visible:
            item = QListWidgetItem()
            self._update_attachment_row(item, attachment)
            item.setData(Qt.ItemDataRole.UserRole, attachment.id)
            if attachment.missing_at is not None:
                item.setForeground(Qt.GlobalColor.darkRed)
            self.attachment_list.addItem(item)
            if attachment.id == selected_id:
                self.attachment_list.setCurrentItem(
                    item, QItemSelectionModel.SelectionFlag.NoUpdate
                )
            if attachment.id in selected_ids:
                item.setSelected(True)
        self.attachment_list.blockSignals(False)
        active_filter = self._attachment_kind != "all"
        self.clear_attachment_filter.setVisible(active_filter)
        self.attachment_filter_status.setText(f"{len(visible)}개 표시 / 전체 {len(attachments)}개")
        self.attachment_filter_status.setVisible(active_filter)
        self.attachment_empty_label.setText(
            f"이 종류의 첨부파일이 없습니다. 전체 첨부는 {len(attachments)}개입니다.\n전체 보기로 확인하세요."
            if attachments
            else "첨부파일이 없습니다. 파일을 추가하거나 끌어놓으세요."
        )
        self.attachment_stack.setCurrentWidget(
            self.attachment_list if visible else self.attachment_empty_label
        )
        self.attachment_drop_hint.setText(
            f"첨부 {len(attachments)}개 · 모든 반복 회차에서 공유"
            if self._task.recurrence_rule
            else f"첨부 {len(attachments)}개"
        )
        self.attachment_drop_hint.setToolTip("Ctrl/Shift로 여러 파일 선택 · 파일을 끌어놓아 추가")
        self._selected_attachment_id = None
        self.attachment_detail.setText(
            "첨부파일이 없습니다."
            if not attachments
            else "첨부파일을 선택하면 저장 정보와 상태를 확인할 수 있습니다."
        )
        self._set_attachment_actions_enabled(False)
        current = self.attachment_list.currentItem()
        self._attachment_selected(current, None)
        if (
            current is not None
            and selected_id is not None
            and previous_selected == self._attachments_by_id.get(selected_id)
            and not was_summary
        ):
            self.attachment_detail.setText(detail)
        if (
            previous_selected is not None
            and current is None
            and previous_selected.id in self._attachments_by_id
        ):
            replacement = self._attachments_by_id[previous_selected.id]
            if replacement.role != previous_selected.role:
                label = {"original": "원본", "work": "작업본", "general": "일반"}[
                    replacement.role.value
                ]
                self.attachment_detail.setText(
                    f"{label}으로 변경됨 · 현재 필터에서 제외. 전체 보기로 확인하세요."
                )
        if added_ids:
            for row in range(self.attachment_list.count()):
                item = self.attachment_list.item(row)
                if item.data(Qt.ItemDataRole.UserRole) in added_ids:
                    self.attachment_list.scrollToItem(item)
                    break
        self._configure_attachment_view()
        self._restore_attachment_anchor(anchor)
        self._schedule_file_samples()
        self._schedule_thumbnails(recheck=True)

    def _update_attachment_row(self, item: QListWidgetItem, attachment: Attachment) -> None:
        role = {"general": "일반", "original": "원본", "work": "작업본"}[attachment.role.value]
        state = []
        sample = self._samples.get(attachment.id_required)
        if attachment.missing_at or (sample and sample.state is AttachmentCheckStatus.MISSING):
            state.append("파일 없음")
        elif sample and sample.state:
            state.append("확인 불가")
        elif self._cache_states.get(attachment.asset_id) == "modified":
            state.append("수정본 있음")
        versions = getattr(self, "_document_counts", {}).get(attachment.family_id, 0)
        if versions:
            state.append(f"보관 {versions}")
        if self.attachment_view_mode == "thumbnails" and attachment.memo.strip():
            state.append("메모 있음")
        set_file_row(
            item,
            attachment.name,
            role,
            " · ".join(state),
            tooltip=self._attachment_information(attachment),
        )

    def _attachment_information(self, attachment: Attachment) -> str:
        sample = self._samples.get(attachment.id_required)
        status = attachment.check_label
        size = attachment.display_size_bytes
        source = "최근 확인" if attachment.observed_at else "첨부 당시"
        if sample and sample.relative_path == attachment.relative_path:
            if sample.state:
                status = (
                    "파일 누락" if sample.state is AttachmentCheckStatus.MISSING else "확인 불가"
                )
            elif sample.size_bytes is not None:
                size, source = sample.size_bytes, "현재 크기"
                if (size, sample.mtime_ns) != (
                    attachment.observed_size_bytes,
                    attachment.observed_mtime_ns,
                ):
                    status = "내용 다시 확인 필요"
        role = {"general": "일반", "original": "원본 · 고정", "work": "작업본"}[
            attachment.role.value
        ]
        text = f"[{role}] {attachment.name}\n크기: {self._format_size(size)} ({source})\n상태: {status}\n최초 SHA-256: {attachment.checksum or '아직 계산되지 않음'}"
        if attachment.name != attachment.original_name:
            text += f"\n처음 첨부한 이름: {attachment.original_name}"
        if self._attachment_service and self._attachment_service.lifecycle:
            versions = getattr(self, "_document_counts", {}).get(attachment.family_id, 0)
            if versions:
                text += f" · 보관본 {versions}개"
            if self._cache_states.get(attachment.asset_id) == "modified":
                text += "\n원본은 유지됩니다. 수정본 저장…에서 변경 내용을 새 작업본으로 남길 수 있습니다."
        if attachment.memo.strip():
            text += "\n메모: " + attachment.memo
        return text

    def _show_attachment_information(self) -> None:
        from shiboken6 import isValid

        if self._selected_attachment_id is None:
            return
        attachment = self._attachments_by_id.get(self._selected_attachment_id)
        if attachment is None:
            return
        self._refresh_source_summary(force=True)
        source = self._source_controller.result if self._source_controller else None
        information = (
            self._attachment_information(attachment)
            + "\n"
            + (
                source.information
                if source
                else "출처 확인 중"
                if self._attachment_service and self._attachment_service.lifecycle
                else "출처 정보 없음"
            )
        )
        self._source_info_attachment = attachment
        self._source_info_token = (
            self._source_controller.current_token if self._source_controller else None
        )
        if self._source_controller:
            self._source_controller.keep_information_request()
        dialog = self._source_info_dialog
        if dialog is not None and isValid(dialog) and dialog.isVisible():
            edit = dialog.findChild(QPlainTextEdit)
            if edit is not None:
                edit.setPlainText(information)
            dialog.raise_()
            dialog.activateWindow()
        else:
            self._source_info_dialog = self._display_attachment_information(information)

    def _display_attachment_information(self, information: str) -> QDialog:
        dialog = QDialog(self)
        dialog.setObjectName("attachmentInformationDialog")
        dialog.setWindowTitle("첨부파일 정보")
        dialog.setStyleSheet(LIGHT_STYLESHEET)
        dialog.resize(560, 400)
        root = QVBoxLayout(dialog)
        text = QPlainTextEdit(information)
        text.setReadOnly(True)
        root.addWidget(text, 1)
        close = QPushButton("닫기")
        close.clicked.connect(dialog.accept)
        root.addWidget(close)
        from officeflow.presentation.document_actions import file_actions

        if self._attachment_service and self._attachment_service.lifecycle:
            file_actions(self, self._attachment_service.lifecycle).keep(dialog)
        else:
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.show()
        return dialog

    def _schedule_file_samples(self) -> None:
        if (
            self._owner_shutting_down
            or self._attachment_service is None
            or not hasattr(self, "attachment_list")
        ):
            return
        self._sample_generation += 1
        self._sample_timer.start()

    def _sample_visible_files(self) -> None:
        if (
            self._owner_shutting_down
            or self._attachment_service is None
            or self.current_tab_key != "attachments"
        ):
            return
        if self._sample_busy:
            if self._sample_worker:
                self._sample_worker.cancel()
            self._sample_timer.start()
            return
        ids = {
            item.data(Qt.ItemDataRole.UserRole) for item in self._selected_attachment_items()[:100]
        }
        for item in visible_items(self.attachment_list):
            ids.add(item.data(Qt.ItemDataRole.UserRole))
            if len(ids) >= 100:
                break
        samples = tuple(
            self._attachments_by_id[key] for key in ids if key in self._attachments_by_id
        )
        generation = self._sample_generation
        if self._sample_worker is None:
            thread = QThread(self)
            worker = _FileSamplesWorker(self._attachment_service)
            worker.moveToThread(thread)
            worker.succeeded.connect(self._apply_file_samples, Qt.ConnectionType.QueuedConnection)
            worker.failed.connect(self._file_samples_failed, Qt.ConnectionType.QueuedConnection)
            worker.finished.connect(self._file_samples_finished, Qt.ConnectionType.QueuedConnection)
            thread.finished.connect(worker.deleteLater)
            self._sample_thread, self._sample_worker = thread, worker
            thread.start()
        self._sample_busy = True
        self._sample_worker.request((generation, samples))

    @Slot(object)
    def _apply_file_samples(self, value: object) -> None:
        if not isinstance(value, tuple) or len(value) != 2:
            return
        generation, samples = value
        service = self._attachment_service
        if generation != self._sample_generation or self._owner_shutting_down or service is None:
            return
        from officeflow.infrastructure.attachments.view_cache import AssetCacheStatus

        refreshed: set[int] = set()
        for sample in samples:
            if (
                isinstance(sample, AssetCacheStatus)
                and service.lifecycle
                and sample.generation == service.lifecycle.cache.generation
            ):
                self._cache_states[sample.asset_id] = sample.state
                refreshed.update(
                    item.id_required
                    for item in self._attachments_by_id.values()
                    if item.asset_id == sample.asset_id
                )
            if isinstance(sample, AttachmentQuickSample):
                current = self._attachments_by_id.get(sample.attachment_id)
                if current and current.relative_path == sample.relative_path:
                    self._samples[sample.attachment_id] = sample
                    refreshed.add(sample.attachment_id)
        with QSignalBlocker(self.attachment_list):
            for row in range(self.attachment_list.count()):
                item = self.attachment_list.item(row)
                identity = item.data(Qt.ItemDataRole.UserRole)
                if identity in refreshed and (current := self._attachments_by_id.get(identity)):
                    self._update_attachment_row(item, current)
        notice = self.attachment_detail.full_text if not self.attachment_detail.is_summary else None
        self._attachment_selected(self.attachment_list.currentItem(), None)
        if notice:
            self.attachment_detail.setText(notice)

    @Slot()
    def _file_samples_finished(self) -> None:
        self._sample_busy = False

    @Slot(object)
    def _file_samples_failed(self, error: object) -> None:
        if not self._owner_shutting_down:
            self.attachment_detail.setToolTip(f"파일 상태를 확인하지 못했습니다: {error}")

    def event(self, event: QEvent) -> bool:
        if event.type() in {QEvent.Type.WindowActivate, QEvent.Type.Show} and hasattr(
            self, "_sample_timer"
        ):
            self._schedule_file_samples()
            self._schedule_thumbnails(recheck=True)
            if hasattr(self, "attachment_list"):
                self._invalidate_source_summary()
                self._refresh_source_summary()
            if event.type() == QEvent.Type.Show:
                self.setGeometry(self._fit_browser_geometry(QRect(self.geometry())))
        if event.type() in {QEvent.Type.Hide, QEvent.Type.WindowStateChange} and hasattr(
            self, "_thumbnail_timer"
        ):
            self._schedule_thumbnails()
        return super().event(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.Resize
            and hasattr(self, "attachment_list")
            and watched is self.attachment_list.viewport()
        ):
            if self.attachment_view_mode == "thumbnails":
                self._configure_attachment_view()
            self._schedule_thumbnails()
        return super().eventFilter(watched, event)

    def _attachment_scroll_anchor(self) -> int | None:
        if not hasattr(self, "attachment_list"):
            return None
        items = visible_items(self.attachment_list)
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None

    def _restore_attachment_anchor(self, identity: int | None) -> None:
        if identity is None:
            return
        self.attachment_list.doItemsLayout()
        for row in range(self.attachment_list.count()):
            item = self.attachment_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == identity:
                self.attachment_list.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtTop)
                break

    def _set_attachment_view(self, mode: str) -> None:
        anchor = self._attachment_scroll_anchor()
        self.attachment_view_mode = mode
        self._image_coordinator.remember_mode(mode)
        self._configure_attachment_view()
        for row in range(self.attachment_list.count()):
            item = self.attachment_list.item(row)
            attachment = self._attachments_by_id.get(item.data(Qt.ItemDataRole.UserRole))
            if attachment:
                self._update_attachment_row(item, attachment)
        self._restore_attachment_anchor(anchor)
        self._schedule_thumbnails(recheck=self._thumbnail_force_epoch != self._thumbnail_epoch)

    def _configure_attachment_view(self) -> None:
        listing = self.attachment_list
        thumbnails = self.attachment_view_mode == "thumbnails"
        self.list_view_button.setChecked(not thumbnails)
        self.thumbnail_view_button.setChecked(thumbnails)
        if thumbnails:
            if not isinstance(listing.itemDelegate(), ThumbnailFileDelegate):
                old = listing.itemDelegate()
                listing.setItemDelegate(
                    ThumbnailFileDelegate(listing, self._image_coordinator, self._thumbnail_client)
                )
                old.deleteLater()
                listing.setViewMode(QListView.ViewMode.IconMode)
            listing.setFlow(QListView.Flow.LeftToRight)
            listing.setWrapping(True)
            listing.setResizeMode(QListView.ResizeMode.Adjust)
            width = max(176, listing.viewport().width())
            columns = min(4, max(1, width // 200))
            cell = QSize(min(240, width // columns), 134 + 2 * (listing.fontMetrics().height() + 4))
            if listing.gridSize() != cell:
                listing.setGridSize(cell)
        else:
            if isinstance(listing.itemDelegate(), ThumbnailFileDelegate):
                old = listing.itemDelegate()
                compact_file_list(listing)
                old.deleteLater()
            listing.setViewMode(QListView.ViewMode.ListMode)
            listing.setFlow(QListView.Flow.TopToBottom)
            listing.setWrapping(False)
            listing.setGridSize(QSize())
        listing.setMovement(QListView.Movement.Static)
        listing.setWordWrap(False)
        listing.setUniformItemSizes(True)
        listing.setDragEnabled(False)
        listing.setAcceptDrops(True)
        listing.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        listing.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)

    def _schedule_thumbnails(self, *, recheck: bool = False) -> None:
        if self._thumbnail_closed or not hasattr(self, "attachment_list"):
            return
        if recheck:
            self._thumbnail_epoch += 1
        if (
            self.attachment_view_mode != "thumbnails"
            or self.current_tab_key != "attachments"
            or not self.isVisible()
            or self.isMinimized()
        ):
            self._thumbnail_timer.stop()
            self._image_coordinator.sync(self._thumbnail_client, ())
            return
        self._thumbnail_timer.start()

    def _load_visible_thumbnails(self) -> None:
        service = self._attachment_service
        if not service or self._thumbnail_closed or self._image_action_pending:
            return
        if (
            not self.isVisible()
            or self.isMinimized()
            or self.current_tab_key != "attachments"
            or self.attachment_view_mode != "thumbnails"
        ):
            self._image_coordinator.sync(self._thumbnail_client, ())
            return
        ratio = self.attachment_list.devicePixelRatioF()
        target = QSize(
            int((self.attachment_list.gridSize().width() - 26) * ratio), int(104 * ratio)
        )
        values = []
        for item in visible_items(self.attachment_list):
            attachment = self._attachments_by_id.get(item.data(Qt.ItemDataRole.UserRole))
            if attachment and is_image_name(attachment.name):
                values.append((service, attachment, target))
        self._image_coordinator.sync(
            self._thumbnail_client,
            tuple(values),
            epoch=self._thumbnail_epoch,
            force=self._thumbnail_force_epoch == self._thumbnail_epoch,
        )

    @Slot(int)
    def _thumbnail_updated(self, client: int) -> None:
        if client == self._thumbnail_client and not self._thumbnail_closed:
            for item in visible_items(self.attachment_list):
                state = self._image_coordinator.state_for(
                    client, item.data(Qt.ItemDataRole.UserRole)
                )
                attachment = self._attachments_by_id.get(item.data(Qt.ItemDataRole.UserRole))
                if state and state.error and attachment:
                    item.setToolTip(
                        self._attachment_information(attachment) + "\n이미지: " + state.error
                    )
            self.attachment_list.viewport().update()

    def _refresh_thumbnails(self) -> None:
        self._thumbnail_epoch += 1
        self._thumbnail_force_epoch = self._thumbnail_epoch
        self._image_coordinator.invalidate(self._thumbnail_client)
        self._schedule_thumbnails()

    def _stop_thumbnails(self) -> None:
        if self._thumbnail_closed:
            return
        self._thumbnail_closed = True
        self._thumbnail_timer.stop()
        self._image_coordinator.unregister(self._thumbnail_client)
        self._image_coordinator.updated.disconnect(self._thumbnail_updated)
        if self._owns_image_coordinator:
            self._image_coordinator.shutdown()

    def _defer_image_action(self, action: Callable[[], None]) -> bool:
        if self._image_action_resuming:
            return False
        if self._image_action_pending:
            return True
        ids = tuple(
            item.data(Qt.ItemDataRole.UserRole) for item in self._selected_attachment_items()
        )
        assets = frozenset(
            self._attachments_by_id[identity].asset_id
            for identity in ids
            if identity in self._attachments_by_id
        )

        def resume() -> None:
            self._image_action_pending = False
            if self._thumbnail_closed or self._owner_shutting_down:
                return
            current = tuple(
                item.data(Qt.ItemDataRole.UserRole) for item in self._selected_attachment_items()
            )
            if current != ids:
                self.attachment_detail.setText(
                    "선택이 바뀌었습니다. 원하는 파일에서 다시 실행하세요."
                )
                return
            self._image_action_resuming = True
            try:
                self._set_attachment_actions_enabled(len(ids) == 1)
                action()
            finally:
                self._image_action_resuming = False

        if self._image_coordinator.after_read(self._thumbnail_client, assets, resume):
            self._image_action_pending = True
            self._set_attachment_actions_enabled(False)
            self.attachment_detail.setText("이미지 읽기를 마무리한 뒤 요청을 실행합니다…")
            return True
        return False

    def _stop_file_samples(self) -> None:
        self._sample_generation += 1
        self._sample_timer.stop()
        if self._sample_worker:
            self._sample_worker.cancel()
        if self._sample_thread:
            self._sample_thread.quit()
            finish_thread(
                self._sample_thread,
                parent=self,
                label="첨부 정보 확인을 안전하게 마무리하고 있습니다…",
            )
            self._sample_thread.deleteLater()
        self._sample_thread = None
        self._sample_worker = None
        self._sample_busy = False

    def _attachment_move_enabled(self, movement: AttachmentMove) -> bool:
        if (
            self._attachment_kind != "all"
            or self._read_only
            or self._image_action_pending
            or self._owner_shutting_down
            or self._attachment_thread is not None
            or self._verification_thread is not None
            or len(self._selected_attachment_items()) != 1
            or (self._export_controller and self._export_controller.busy)
        ):
            return False
        try:
            if self._task_service.get_including_deleted(self._task_id).deleted_at is not None:
                return False
        except LookupError:
            return False
        row = self.attachment_list.currentRow()
        if row < 0 or self._selected_attachment_id is None:
            return False
        return (
            row > 0
            if movement in (AttachmentMove.UP, AttachmentMove.FIRST)
            else row < self.attachment_list.count() - 1
        )

    def _move_attachment(self, movement: AttachmentMove) -> None:
        if (
            not self._attachment_move_enabled(movement)
            or self._attachment_service is None
            or self._selected_attachment_id is None
        ):
            return
        try:
            self._attachment_service.move_attachment(
                self._task_id, self._selected_attachment_id, movement
            )
        except Exception as error:
            self.attachment_detail.setText(f"첨부 순서를 변경하지 못했습니다: {error}")
        self._attachments_changed(self._task_id)
        self._refresh_pending_attachments()
        self.attachment_list.setFocus(Qt.FocusReason.OtherFocusReason)

    def _build_attachment_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction("열기", self._open_attachment).setEnabled(
            self.open_attachment_button.isEnabled()
        )
        menu.addAction(self.save_attachment_button.text(), self._save_attachment_copy).setEnabled(
            self.save_attachment_button.isEnabled()
        )
        if len(self._selected_attachment_items()) > 1:
            menu.addAction(
                self.delete_attachment_button.text(), self._delete_attachment
            ).setEnabled(self._attachment_delete_enabled())
            return menu
        menu.addAction("이미지 확대…", self._show_image_preview).setEnabled(
            self.image_preview_button.isEnabled()
        )
        if self._attachment_service and self._attachment_service.lifecycle:
            for label, operation in (
                ("새 작업본…", "create_work"),
                ("보관본 남기기…", "create_version"),
                ("원본으로 지정…", "designate_original"),
                ("원본 지정 해제…", "release_original"),
            ):

                def create_action(_checked: bool = False, value: str = operation) -> None:
                    self._create_document(value)

                menu.addAction(label, create_action).setEnabled(
                    self._document_action_enabled(operation)
                )
            menu.addAction("보관함…", self._open_document_browser)
            menu.addAction("관련 파일 보기…", self._open_related_files).setEnabled(
                self._selected_attachment_family() is not None
            )
            menu.addAction("수정본 확인/저장…", self._open_view_copies)
        menu.addAction("이름 변경…", lambda: self._edit_attachment_metadata()).setEnabled(
            self._attachment_metadata_enabled()
        )
        menu.addAction("메모…", lambda: self._edit_attachment_metadata(edit_memo=True)).setEnabled(
            self._attachment_metadata_enabled()
        )
        menu.addAction("파일 정보…", self._show_attachment_information).setEnabled(
            self.attachment_info_button.isEnabled()
        )
        for button in (
            self.verify_attachment_button,
            self.unlink_attachment_button,
            self.delete_attachment_button,
        ):
            menu.addAction(button.text(), button.click).setEnabled(button.isEnabled())
        menu.addSeparator()
        for label, movement in (
            ("위로", AttachmentMove.UP),
            ("아래로", AttachmentMove.DOWN),
            ("맨 위로", AttachmentMove.FIRST),
            ("맨 아래로", AttachmentMove.LAST),
        ):

            def move_action(_checked: bool = False, move: AttachmentMove = movement) -> None:
                self._move_attachment(move)

            action = menu.addAction(label, move_action)
            action.setEnabled(self._attachment_move_enabled(movement))
            if self._attachment_kind != "all":
                action.setToolTip("전체 파일 보기에서 순서를 바꿀 수 있습니다.")
        return menu

    def _document_action_enabled(self, operation: str) -> bool:
        if (
            not self._attachment_metadata_enabled()
            or self._attachment_service is None
            or self._selected_attachment_id is None
        ):
            return False
        attachment = self._attachment_service.get_metadata(self._selected_attachment_id)
        if operation == "release_original":
            return attachment.role.value == "original"
        return operation == "create_work" or not attachment.is_frozen

    def _attachment_metadata_enabled(self) -> bool:
        if (
            self._attachment_filter_busy()
            or self._read_only
            or self._image_action_pending
            or self._owner_shutting_down
            or self._attachment_service is None
            or self._attachment_thread is not None
            or self._verification_thread is not None
            or self._selected_attachment_id is None
            or len(self._selected_attachment_items()) != 1
            or (self._export_controller and self._export_controller.busy)
        ):
            return False
        try:
            return self._task_service.get_including_deleted(self._task_id).deleted_at is None
        except LookupError:
            return False

    def _edit_attachment_metadata(self, *, edit_memo: bool = False) -> None:
        if (
            not self._attachment_metadata_enabled()
            or self._attachment_service is None
            or self._selected_attachment_id is None
        ):
            return
        service = self._attachment_service
        try:
            attachment = service.get_metadata(self._selected_attachment_id)

            def submit(value: str) -> None:
                if edit_memo:
                    service.update_memo(
                        attachment.id_required,
                        value,
                        expected_revision=attachment.metadata_revision,
                    )
                else:
                    service.rename_attachment(
                        attachment.id_required,
                        value,
                        expected_revision=attachment.metadata_revision,
                    )

            editor = AttachmentMetadataDialog(
                attachment, edit_memo=edit_memo, parent=self, submit=submit
            )
            editor.exec()
        except (ValueError, LookupError, AttachmentOperationError) as error:
            QMessageBox.warning(self, "첨부 정보를 변경하지 못했습니다.", str(error))
        self._refresh_pending_attachments()

    def _attachment_context_menu(self, point: QPoint) -> None:
        item = self.attachment_list.itemAt(point)
        if item is None:
            return
        if not item.isSelected():
            self.attachment_list.clearSelection()
            self.attachment_list.setCurrentItem(item)
            item.setSelected(True)
        menu = self._build_attachment_context_menu()
        menu.exec(self.attachment_list.viewport().mapToGlobal(point))
        menu.deleteLater()

    def select_attachment(self, attachment_id: int) -> None:
        if self._attachment_service is None:
            return
        if self._attachment_kind != "all":
            self._attachment_kind = "all"
            self._refresh_attachments()
        for index in range(self.attachment_list.count()):
            if self.attachment_list.item(index).data(Qt.ItemDataRole.UserRole) == attachment_id:
                self.attachment_list.setCurrentRow(
                    index, QItemSelectionModel.SelectionFlag.ClearAndSelect
                )
                return

    def _save_attachment_copy(self) -> None:
        if self._defer_image_action(self._save_attachment_copy):
            return
        if self._export_controller is None:
            return
        references = tuple(
            FileReference.attachment(item.data(Qt.ItemDataRole.UserRole))
            for item in self._selected_attachment_items()
        )
        self._export_controller.choose(references)

    def _open_clicked_attachment(self, item: QListWidgetItem) -> None:
        self.attachment_list.clearSelection()
        self.attachment_list.setCurrentItem(item)
        item.setSelected(True)
        self._open_attachment()

    def _choose_attachment(self) -> None:
        sources, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            "첨부할 파일 선택 (여러 개 가능)",
            "",
            "모든 파일 (*.*)",
        )
        if not sources:
            return
        self._add_attachment_sources(tuple(Path(source) for source in sources))

    @Slot(object)
    def _add_attachment_sources(self, sources_value: object) -> None:
        if self._verification_thread is not None:
            return
        if self._read_only or self._attachment_service is None:
            return
        if self._attachment_thread is not None:
            QMessageBox.information(
                self,
                "첨부파일 복사 중",
                "현재 파일 복사가 끝난 뒤 다시 추가해 주세요.",
            )
            return
        if not isinstance(sources_value, tuple):
            return
        unique_sources: list[Path] = []
        source_sizes: dict[Path, int] = {}
        seen: set[str] = set()
        rejected: list[str] = []
        for value in sources_value:
            if not isinstance(value, Path):
                continue
            try:
                source = value.expanduser().resolve()
                if not source.is_file():
                    rejected.append(f"{value.name or value}: 파일이 아닙니다.")
                    continue
                size_bytes = source.stat().st_size
            except OSError as error:
                rejected.append(f"{value.name or value}: {error}")
                continue
            key = str(source).casefold()
            if key in seen:
                continue
            seen.add(key)
            unique_sources.append(source)
            source_sizes[source] = size_bytes
        if rejected:
            preview = "\n".join(rejected[:5])
            remainder = len(rejected) - 5
            if remainder > 0:
                preview += f"\n외 {remainder}개"
            QMessageBox.warning(
                self,
                "일부 파일을 첨부할 수 없습니다.",
                preview,
            )
        if not unique_sources:
            return
        large_sources = [
            source for source in unique_sources if source_sizes[source] >= 100 * 1024 * 1024
        ]
        if large_sources:
            total_size = sum(source_sizes[source] for source in large_sources)
            answer = QMessageBox.question(
                self,
                "큰 첨부파일",
                f"100 MB 이상 파일 {len(large_sources)}개의 합계 크기는 "
                f"{self._format_size(total_size)}입니다.\n"
                "백업 크기와 다른 PC로 옮기는 시간이 크게 늘어날 수 있습니다. "
                "그래도 첨부할까요?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._start_attachment_imports(tuple(unique_sources))

    def _start_attachment_import(self, source: Path) -> None:
        self._start_attachment_imports((source,))

    def _start_attachment_imports(self, sources: tuple[Path, ...]) -> None:
        if not sources or self._attachment_service is None or self._attachment_thread is not None:
            return
        thread = QThread(self)
        worker = AttachmentImportWorker(self._attachment_service, self._task_id, sources)
        worker.moveToThread(thread)
        file_label = sources[0].name if len(sources) == 1 else f"파일 {len(sources)}개"
        progress = QProgressDialog(
            f"{file_label}를 안전하게 복사하고 있습니다.",
            "취소",
            0,
            len(sources),
            self,
        )
        progress.setWindowTitle("첨부파일 복사")
        progress.setWindowModality(Qt.WindowModality.NonModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.canceled.connect(lambda: worker.cancel())
        thread.started.connect(worker.run)
        worker.fileStarted.connect(self._attachment_import_started)
        worker.succeeded.connect(self._attachment_imported)
        worker.failed.connect(self._attachment_import_failed)
        worker.canceled.connect(self._attachment_import_was_canceled)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._attachment_import_finished)
        self._attachment_thread = thread
        self._attachment_worker = worker
        self._attachment_progress = progress
        self._attachment_import_successes = 0
        self._attachment_import_failures = []
        self._attachment_import_canceled = False
        self.add_attachment_button.setEnabled(False)
        self.attachment_list.setAcceptDrops(False)
        self._set_attachment_actions_enabled(False)
        progress.show()
        thread.start()

    def reject(self) -> None:
        if self.folder_panel:
            self.folder_panel.shutdown()
        if self._defer_close_for_verification() or self._defer_close_for_attachment_import():
            return
        if self._export_controller:
            self._export_controller.shutdown()
        from officeflow.presentation.document_actions import stop_file_actions

        stop_file_actions(self)
        self._stop_file_samples()
        if self._source_controller:
            self._source_controller.shutdown()
        super().reject()

    def shutdown(self) -> None:
        """Finish a modeless dialog's import before its owner disposes the DB."""
        self._close_image_dialogs()
        self._stop_thumbnails()
        if self.folder_panel:
            self.folder_panel.shutdown()
        from officeflow.presentation.document_actions import stop_file_actions

        stop_file_actions(self)
        self._owner_shutting_down = True
        if self._source_controller:
            self._source_controller.shutdown()
        self._stop_file_samples()
        if self._export_controller:
            self._export_controller.shutdown()
        if hasattr(self, "_unsubscribe_attachments"):
            self._unsubscribe_attachments()
        self._attachment_refresh_timer.stop()
        if self._verification_worker is not None:
            self._verification_worker.cancel()
        if self._verification_thread is not None:
            self._verification_thread.quit()
            finish_thread(
                self._verification_thread,
                parent=self,
                label="파일 검사를 취소하고 마무리하고 있습니다…",
            )
        self._close_when_attachment_finishes = False
        if self._attachment_worker is not None:
            self._attachment_worker.cancel()
        if self._attachment_thread is not None:
            self._attachment_thread.quit()
            finish_thread(
                self._attachment_thread,
                parent=self,
                label="첨부 복사를 취소하고 마무리하고 있습니다…",
            )
        self._attachment_thread = None
        self._attachment_worker = None
        if self._attachment_progress is not None:
            self._attachment_progress.close()
        self._attachment_progress = None
        self.reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._close_image_dialogs()
        if self.folder_panel:
            self.folder_panel.shutdown()
        if self._defer_close_for_verification() or self._defer_close_for_attachment_import():
            event.ignore()
            return
        from officeflow.presentation.document_actions import stop_file_actions

        stop_file_actions(self)
        if self._export_controller:
            self._export_controller.shutdown()
        self._stop_file_samples()
        super().closeEvent(event)

    def _defer_close_for_attachment_import(self) -> bool:
        thread = self._attachment_thread
        if thread is None or not thread.isRunning():
            return False
        self._close_when_attachment_finishes = True
        if self._attachment_worker is not None:
            self._attachment_worker.cancel()
        if self._attachment_progress is not None:
            self._attachment_progress.setLabelText(
                "첨부파일 복사를 취소하고 안전하게 마무리하고 있습니다."
            )
            self._attachment_progress.setCancelButton(None)
        return True

    @Slot(object)
    def _attachment_imported(self, _attachment: object) -> None:
        self._attachment_import_successes += 1
        if isinstance(_attachment, Attachment) and _attachment.operation_warning:
            self._attachment_import_warnings.append(
                f"{_attachment.original_name}: {_attachment.operation_warning}"
            )
        if self._attachment_progress is not None:
            self._attachment_progress.setValue(
                self._attachment_import_successes + len(self._attachment_import_failures)
            )

    @Slot(int, int, str)
    def _attachment_import_started(self, index: int, total: int, name: str) -> None:
        if self._attachment_progress is not None:
            self._attachment_progress.setLabelText(
                f"{index}/{total}  '{name}' 파일을 안전하게 복사하고 있습니다."
            )

    @Slot(object, object)
    def _attachment_import_failed(self, source: object, error: object) -> None:
        message = str(error) if isinstance(error, Exception) else "알 수 없는 오류"
        source_path = source if isinstance(source, Path) else Path("알 수 없는 파일")
        self._attachment_import_failures.append((source_path, message))
        if self._attachment_progress is not None:
            self._attachment_progress.setValue(
                self._attachment_import_successes + len(self._attachment_import_failures)
            )

    @Slot()
    def _attachment_import_was_canceled(self) -> None:
        self._attachment_import_canceled = True

    @Slot()
    def _attachment_import_finished(self) -> None:
        if self._owner_shutting_down:
            return
        if self._attachment_progress is not None:
            self._attachment_progress.close()
        if hasattr(self, "add_attachment_button"):
            self.add_attachment_button.setEnabled(not self._read_only)
        if hasattr(self, "attachment_list"):
            self.attachment_list.setAcceptDrops(not self._read_only)
        thread = self._attachment_thread
        self._attachment_thread = None
        self._attachment_worker = None
        self._attachment_progress = None
        if thread is not None:
            thread.deleteLater()
        successes = self._attachment_import_successes
        failures = tuple(self._attachment_import_failures)
        canceled = self._attachment_import_canceled
        if successes:
            self._refresh_attachments()
            self.changed.emit()
            self.attachment_detail.setText(f"첨부 완료: {successes}개 파일")
            if self._attachment_import_warnings:
                self.attachment_detail.setText(
                    self.attachment_detail.text()
                    + "\n"
                    + "\n".join(self._attachment_import_warnings[:5])
                )
        if failures and not self._close_when_attachment_finishes:
            lines = [f"{source.name}: {message}" for source, message in failures[:5]]
            remainder = len(failures) - 5
            if remainder > 0:
                lines.append(f"외 {remainder}개")
            QMessageBox.warning(
                self,
                f"첨부 실패 {len(failures)}개",
                "나머지 파일은 계속 처리했습니다.\n\n" + "\n".join(lines),
            )
        elif canceled and successes and not self._close_when_attachment_finishes:
            self.attachment_detail.setText(f"첨부 {successes}개 완료 · 나머지 파일은 취소됨")
        self._attachment_import_successes = 0
        self._attachment_import_failures = []
        self._attachment_import_canceled = False
        self._attachment_import_warnings = []
        self._refresh_pending_attachments()
        if self._close_when_attachment_finishes:
            self._close_when_attachment_finishes = False
            QTimer.singleShot(0, self._finish_deferred_close)

    def _finish_deferred_close(self) -> None:
        QDialog.reject(self)

    def _attachment_selected(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        selected = self._selected_attachment_items()
        self._update_image_preview()
        count = len(selected)
        self.preserve_view_button.hide()
        self.attachment_warning.hide()
        self.attachment_kind_combo.setEnabled(not self._attachment_filter_busy())
        self.clear_attachment_filter.setEnabled(not self._attachment_filter_busy())
        if hasattr(self, "document_browser_button"):
            self.document_browser_button.setToolTip(
                "선택 파일의 관련 첨부·보관본" if count == 1 else "이 업무의 보관함"
            )
        if count > 1:
            self._selected_attachment_id = None
            self._refresh_source_summary()
            self.attachment_detail.setToolTip("")
            self.attachment_detail.set_summary(f"{count}개 선택")
            self.delete_attachment_button.setText(f"선택 {count}개 삭제…")
            self._set_attachment_actions_enabled(False)
            self.more_attachment_button.setEnabled(self._attachment_delete_enabled())
            self.save_attachment_button.setText(f"선택 {count}개 저장…")
            self.save_attachment_button.setEnabled(
                self._export_controller is not None
                and not self._export_controller.busy
                and self._verification_thread is None
            )
            return
        self.save_attachment_button.setText("다른 이름으로 저장…")
        self.delete_attachment_button.setText("파일 삭제")
        if current is None or count == 0:
            self._selected_attachment_id = None
            self._refresh_source_summary()
            self.attachment_detail.set_summary(
                "파일을 선택하세요."
                if self.attachment_list.count()
                else "첨부파일이 없습니다."
                if not self._attachments_by_id
                else "선택한 종류에 파일이 없습니다."
            )
            self.attachment_detail.setToolTip("")
            self._set_attachment_actions_enabled(False)
            return
        current = selected[0]
        attachment_id = current.data(Qt.ItemDataRole.UserRole)
        attachment = self._attachments_by_id.get(attachment_id)
        if attachment is None:
            return
        self._selected_attachment_id = attachment.id
        self._refresh_source_summary()
        self._render_attachment_summary()
        warnings = []
        if self._cache_states.get(attachment.asset_id) == "modified":
            warnings.append("수정본 있음 · 수정한 내용은 따로 저장하세요.")
            self.preserve_view_button.setVisible(not self._read_only)
        sample = self._samples.get(attachment.id_required)
        if attachment.missing_at or (sample and sample.state is AttachmentCheckStatus.MISSING):
            warnings.append("파일 없음 · 파일 정보를 확인하세요.")
        elif sample and sample.state:
            warnings.append("파일 확인 불가 · 파일 정보를 확인하세요.")
        if warnings:
            self.attachment_warning.setText("\n".join(warnings))
            self.attachment_warning.show()
        self.attachment_detail.setToolTip(self._attachment_information(attachment))
        self._set_attachment_actions_enabled(True)

    def _open_attachment(self) -> None:
        if self._defer_image_action(self._open_attachment):
            return
        if (
            self._selected_attachment_id is None
            or self._attachment_service is None
            or len(self._selected_attachment_items()) != 1
        ):
            return
        if self._attachment_service.lifecycle:
            from officeflow.presentation.document_actions import file_actions

            file_actions(self, self._attachment_service.lifecycle).open(
                "attachment", self._selected_attachment_id, feedback=self.attachment_detail
            )
            return
        try:
            path = self._attachment_service.path_for_open(self._selected_attachment_id)
        except (AttachmentOperationError, LookupError) as error:
            QMessageBox.warning(self, "첨부파일을 열지 못했습니다.", str(error))
            self._refresh_attachments()
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(
                self,
                "첨부파일을 열지 못했습니다.",
                "이 파일 형식을 열 수 있는 Windows 앱을 찾지 못했습니다.",
            )

    def _verify_attachment(self) -> None:
        if self._defer_image_action(self._verify_attachment):
            return
        if self._selected_attachment_id is None or self._attachment_service is None:
            return
        if self._verification_thread is not None or self._attachment_thread is not None:
            return
        service = self._attachment_service
        attachment_id = self._selected_attachment_id
        thread = QThread(self)
        worker = OperationWorker(
            lambda canceled: service.verify(
                attachment_id,
                cancel_requested=canceled,
                progress=worker.progress.emit,
            )
        )
        worker.moveToThread(thread)
        progress = QProgressDialog("첨부파일 내용을 확인하고 있습니다…", "취소", 0, 100, self)
        progress.setWindowModality(Qt.WindowModality.NonModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.canceled.connect(worker.cancel, Qt.ConnectionType.DirectConnection)
        worker.progress.connect(
            lambda done, total: progress.setValue(min(100, int(done * 100 / max(1, total))))
        )
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._verification_succeeded)
        worker.failed.connect(self._verification_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._verification_finished)
        thread.finished.connect(thread.deleteLater)
        self._verification_thread = thread
        self._verification_worker = worker
        self._verification_progress = progress
        self._verification_attachment_id = attachment_id
        self._set_attachment_actions_enabled(False)
        self.add_attachment_button.setEnabled(False)
        self.attachment_list.setAcceptDrops(False)
        progress.show()
        thread.start()

    def _verification_succeeded(self, value: object) -> None:
        if self._owner_shutting_down or not isinstance(value, AttachmentVerification):
            return
        result = value
        selected_id = self._selected_attachment_id
        self._refresh_attachments()
        if selected_id is not None:
            self.select_attachment(selected_id)
        if result.integrity is AttachmentIntegrity.VERIFIED:
            message = (
                "내용 확인됨 · 최초 기준 없음"
                if result.attachment.checksum is None
                else "확인 완료: 첨부 당시와 내용이 같습니다."
            )
        elif result.integrity is AttachmentIntegrity.MODIFIED:
            message = "첨부 후 수정됨 · 편집하여 저장한 현재 내용입니다. 변경만으로 파일 손상이라고 판단하지 않습니다."
        else:
            message = "파일 누락: 관리 폴더에서 파일을 찾지 못했습니다."
        self.attachment_detail.setText(message)
        self._display_attachment_information(
            message + "\n\n" + self._attachment_information(result.attachment)
        )

    def _verification_failed(self, error: object) -> None:
        if not self._owner_shutting_down:
            self.attachment_detail.setText(f"무결성 확인: {error}")
            if not isinstance(error, AttachmentCanceledError):
                attachment = self._attachments_by_id.get(self._verification_attachment_id or -1)
                information = self._attachment_information(attachment) if attachment else ""
                self._display_attachment_information(f"내용 확인 실패: {error}\n\n{information}")

    def _verification_finished(self) -> None:
        if self._verification_progress is not None:
            self._verification_progress.close()
            self._verification_progress.deleteLater()
        self._verification_progress = None
        self._verification_worker = None
        self._verification_thread = None
        self._verification_attachment_id = None
        self._refresh_pending_attachments()
        self.add_attachment_button.setEnabled(not self._read_only)
        self.attachment_list.setAcceptDrops(not self._read_only)
        self._set_attachment_actions_enabled(self._selected_attachment_id is not None)
        if self._close_when_verification_finishes and not self._owner_shutting_down:
            QDialog.reject(self)

    def _defer_close_for_verification(self) -> bool:
        if self._verification_thread is None or not self._verification_thread.isRunning():
            return False
        self._close_when_verification_finishes = True
        if self._verification_worker is not None:
            self._verification_worker.cancel()
        if self._verification_progress is not None:
            self._verification_progress.setLabelText("검사를 취소하고 마무리하고 있습니다…")
        return True

    def _unlink_attachment(self) -> None:
        if self._defer_image_action(self._unlink_attachment):
            return
        if self._selected_attachment_id is None or self._attachment_service is None:
            return
        attachment = self._attachments_by_id.get(self._selected_attachment_id)
        if attachment is None:
            return
        answer = QMessageBox.question(
            self,
            "첨부 연결 해제",
            f"'{attachment.name}'을 정리 대기함으로 옮길까요?\n"
            "파일은 삭제되지 않으며 데이터 관리에서 복원하거나 영구 삭제할 수 있습니다.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            retained_path = self._attachment_service.unlink(attachment.id_required)
        except (AttachmentOperationError, LookupError) as error:
            QMessageBox.warning(self, "연결을 해제하지 못했습니다.", str(error))
            return
        self._refresh_attachments()
        self.changed.emit()
        QMessageBox.information(
            self,
            "연결 해제 완료",
            f"파일은 삭제하지 않고 정리 대기함으로 옮겼습니다.\n보존 위치: {retained_path}",
        )

    def _delete_attachment(self) -> None:
        if self._defer_image_action(self._delete_attachment):
            return
        if not self._attachment_delete_enabled() or self._attachment_service is None:
            return
        selected = self._selected_attachment_items()
        if len(selected) > 1 and self._attachment_service.lifecycle:
            from officeflow.application.attachment_deletion import DeleteTarget
            from officeflow.presentation.document_actions import file_actions

            targets = tuple(
                DeleteTarget("attachment", item.data(Qt.ItemDataRole.UserRole)) for item in selected
            )
            file_actions(self, self._attachment_service.lifecycle).delete_many(
                self._task_id, targets, self._document_changed
            )
            return
        if self._selected_attachment_id is None or self._attachment_service is None:
            return
        attachment = self._attachments_by_id.get(self._selected_attachment_id)
        if attachment is None:
            return
        if self._attachment_service.lifecycle:
            from officeflow.presentation.document_actions import file_actions

            file_actions(self, self._attachment_service.lifecycle).delete(
                "attachment",
                attachment.id_required,
                self._document_changed,
            )
            return
        answer = QMessageBox.warning(
            self,
            "첨부파일 영구 삭제",
            f"'{attachment.name}'의 연결을 영구 삭제합니다.\n다른 보관 참조가 없는 실제 파일만 삭제됩니다.\n이 동작은 되돌릴 수 없습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._attachment_service.delete_file(attachment.id_required)
        except (AttachmentOperationError, LookupError, OSError, FileInUseError) as error:
            QMessageBox.warning(self, "첨부파일을 삭제하지 못했습니다.", str(error))
            self._refresh_attachments()
            return
        self._refresh_attachments()
        self.changed.emit()

    def _attachment_delete_enabled(self) -> bool:
        count = len(self._selected_attachment_items())
        if (
            self._read_only
            or self._image_action_pending
            or self._owner_shutting_down
            or not 1 <= count <= 100
            or self._attachment_service is None
            or (count > 1 and self._attachment_service.lifecycle is None)
            or self._verification_thread is not None
            or self._attachment_thread is not None
            or (self._export_controller and self._export_controller.busy)
            or getattr(self.property("documentActionsController"), "_job_thread", None) is not None
        ):
            return False
        try:
            return self._task_service.get_including_deleted(self._task_id).deleted_at is None
        except LookupError:
            return False

    def _set_attachment_actions_enabled(self, enabled: bool) -> None:
        self.attachment_kind_combo.setEnabled(not self._attachment_filter_busy())
        self.clear_attachment_filter.setEnabled(not self._attachment_filter_busy())
        selected = self._attachments_by_id.get(self._selected_attachment_id or -1)
        self.image_preview_button.setEnabled(
            enabled and selected is not None and is_image_name(selected.name)
        )
        self.image_expand_action.setEnabled(self.image_preview_button.isEnabled())
        self.clipboard_image_button.setEnabled(
            not self._read_only
            and self._attachment_thread is None
            and self._verification_thread is None
            and not self._owner_shutting_down
        )
        enabled = (
            enabled
            and not self._attachment_filter_busy()
            and not self._image_action_pending
            and self._verification_thread is None
            and self._attachment_thread is None
            and not (self._export_controller and self._export_controller.busy)
        )
        self.open_attachment_button.setEnabled(enabled)
        self.attachment_info_button.setEnabled(enabled)
        self.verify_attachment_button.setEnabled(enabled)
        self.unlink_attachment_button.setEnabled(enabled and not self._read_only)
        self.delete_attachment_button.setEnabled(self._attachment_delete_enabled())
        self.save_attachment_button.setEnabled(enabled and self._export_controller is not None)
        self.more_attachment_button.setEnabled(
            not self._owner_shutting_down and not self._image_action_pending
        )

    def _add_clipboard_image(self) -> None:
        if (
            self._read_only
            or self._attachment_service is None
            or self._attachment_thread
            or self._verification_thread
        ):
            return
        if self._clipboard_dialog and self._clipboard_dialog.isVisible():
            self._clipboard_dialog.raise_()
            return
        dialog = ClipboardAttachmentDialog(self._attachment_service, self._task_id, self)
        self._clipboard_dialog = dialog
        dialog.changed.connect(self._refresh_attachments)
        dialog.changed.connect(self.changed)
        dialog.notice.connect(self.attachment_detail.setText)
        dialog.finished.connect(lambda _result: dialog.deleteLater())
        dialog.finished.connect(lambda _result: setattr(self, "_clipboard_dialog", None))
        dialog.show()

    def _show_image_preview(self) -> None:
        if self._image_preview is None:
            self._image_preview = ImagePreviewDialog(self, coordinator=self._image_coordinator)
        self._image_preview.show()
        self._image_preview.raise_()
        self._update_image_preview()

    def _update_image_preview(self) -> None:
        viewer = self._image_preview
        if viewer is None or not viewer.isVisible():
            return
        if self._image_action_pending:
            viewer.clear("파일 작업을 기다리는 중입니다…")
            return
        selected = self._selected_attachment_items()
        attachment = (
            self._attachments_by_id.get(selected[0].data(Qt.ItemDataRole.UserRole))
            if len(selected) == 1
            else None
        )
        if (
            attachment is None
            or self._attachment_service is None
            or not is_image_name(attachment.name)
        ):
            viewer.clear()
        else:
            viewer.load(self._attachment_service, attachment.id_required, attachment.name)

    def _close_image_dialogs(self) -> None:
        if self._clipboard_dialog:
            self._clipboard_dialog.reject()
        if self._image_preview:
            self._image_preview.reject()

    def done(self, result: int) -> None:
        if self._defer_close_for_verification() or self._defer_close_for_attachment_import():
            return
        self._owner_shutting_down = True
        self._attachment_refresh_timer.stop()
        self._close_image_dialogs()
        self._stop_thumbnails()
        self._stop_file_samples()
        self._finish_browser_state()
        super().done(result)

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        if size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


class WorkLogBrowserDialog(QDialog):
    changed = Signal()
    exportDirectoryChanged = Signal(str)
    windowSizeChanged = Signal(int, int)
    SEARCH_BATCH_SIZE = 25

    def __init__(
        self,
        *,
        task_service: TaskService,
        record_service: RecordService,
        attachment_service: AttachmentService | None = None,
        attachment_export_service: AttachmentExportService | None = None,
        attachment_export_directory: str | None = None,
        folder_link_service: FolderLinkService | None = None,
        image_coordinator: AttachmentImageCoordinator | None = None,
        record_window_size: Callable[[], tuple[int | None, int | None]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._task_service = task_service
        self._record_service = record_service
        self._attachment_service = attachment_service
        self._record_dialogs: list[TaskRecordsDialog] = []
        self._record_window_size = record_window_size
        self._attachment_export_service = attachment_export_service
        self._attachment_export_directory = attachment_export_directory
        self._folder_link_service = folder_link_service
        self._image_coordinator = image_coordinator or AttachmentImageCoordinator(self)
        self._owns_image_coordinator = image_coordinator is None
        self._groups_by_key: dict[tuple[str, int], _RecordGroup] = {}
        self._selected_group_key: tuple[str, int] | None = None
        self._selected_task_context: tuple[int, datetime | None] | None = None
        self._loaded_completed: list[Task] = []
        self._loaded_logs: list[WorkLog] = []
        self._completed_offset = 0
        self._log_offset = 0
        self.setWindowTitle("날짜별 업무일지")
        self.setModal(False)
        self.resize(680, 560)
        self.setMinimumSize(500, 440)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        title = QLabel("업무일지")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("workLogSearch")
        self.search_edit.setPlaceholderText("전체 기간에서 검색: 제목, 내용, 첨부파일명")
        self.search_edit.setClearButtonEnabled(True)
        root.addWidget(self.search_edit)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._refresh)
        self.search_edit.textChanged.connect(self._search_timer.start)
        self.range_controls_widget = QWidget()
        range_controls = QHBoxLayout(self.range_controls_widget)
        range_controls.setContentsMargins(0, 0, 0, 0)
        self.range_checkbox = QCheckBox("기간 제한 (끄면 전체 기간)")
        self.range_checkbox.setObjectName("workLogRangeEnabled")
        self.range_checkbox.toggled.connect(self._refresh)
        range_controls.addWidget(self.range_checkbox)
        self.range_from_edit = QDateEdit(QDate.currentDate().addYears(-1))
        self.range_from_edit.setObjectName("workLogRangeFrom")
        self.range_from_edit.setCalendarPopup(True)
        self.range_from_edit.setDisplayFormat("yyyy-MM-dd")
        self.range_from_edit.dateChanged.connect(self._refresh)
        range_controls.addWidget(date_with_weekday(self.range_from_edit), 1)
        range_controls.addWidget(QLabel("~"))
        self.range_to_edit = QDateEdit(QDate.currentDate())
        self.range_to_edit.setObjectName("workLogRangeTo")
        self.range_to_edit.setCalendarPopup(True)
        self.range_to_edit.setDisplayFormat("yyyy-MM-dd")
        self.range_to_edit.dateChanged.connect(self._refresh)
        range_controls.addWidget(date_with_weekday(self.range_to_edit), 1)
        root.addWidget(self.range_controls_widget)
        self.day_controls_widget = QWidget()
        controls = QHBoxLayout(self.day_controls_widget)
        controls.setContentsMargins(0, 0, 0, 0)
        self.previous_button = QPushButton("이전 날")
        self.previous_button.clicked.connect(lambda: self._move_date(-1))
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setObjectName("workLogBrowserDate")
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.dateChanged.connect(self._refresh)
        self.next_button = QPushButton("다음 날")
        self.next_button.clicked.connect(lambda: self._move_date(1))
        controls.addWidget(self.previous_button)
        controls.addWidget(date_with_weekday(self.date_edit), 1)
        controls.addWidget(self.next_button)
        root.addWidget(self.day_controls_widget)

        self.result_count_label = QLabel()
        self.result_count_label.setObjectName("workLogResultCount")
        self.result_count_label.setProperty("muted", True)
        root.addWidget(self.result_count_label)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("workLogBrowserList")
        self.list_widget.currentItemChanged.connect(self._show_selected)
        self.list_widget.itemDoubleClicked.connect(lambda _item: self._open_selected_records())
        root.addWidget(self.list_widget, 1)
        self.load_more_button = QPushButton("업무 더 보기")
        self.load_more_button.setObjectName("workLogLoadMoreButton")
        self.load_more_button.clicked.connect(self._load_more_search)
        self.load_more_button.hide()
        root.addWidget(self.load_more_button, alignment=Qt.AlignmentFlag.AlignCenter)
        self.detail = QTextEdit()
        self.detail.setObjectName("workLogBrowserDetail")
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(220)
        self.detail.setPlainText("날짜를 선택하면 기록을 확인할 수 있습니다.")
        root.addWidget(self.detail)
        self.open_records_button = QPushButton("업무 보기")
        self.open_records_button.setObjectName("workLogOpenRecordsButton")
        self.open_records_button.setEnabled(False)
        self.open_records_button.clicked.connect(self._open_selected_records)
        root.addWidget(self.open_records_button, alignment=Qt.AlignmentFlag.AlignRight)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._refresh()

    def _move_date(self, days: int) -> None:
        current = cast(date, self.date_edit.date().toPython())
        self.date_edit.setDate(_qdate(current + timedelta(days=days)))

    def _refresh(self, _selected: QDate | None = None) -> None:
        selected_date = cast(date, self.date_edit.date().toPython())
        search = self.search_edit.text().strip()
        searching = bool(search)
        self.previous_button.setEnabled(not searching)
        self.date_edit.setEnabled(not searching)
        self.next_button.setEnabled(not searching)
        self.day_controls_widget.setVisible(not searching)
        self.range_controls_widget.setVisible(searching)
        self.range_checkbox.setEnabled(searching)
        range_enabled = searching and self.range_checkbox.isChecked()
        self.range_from_edit.setEnabled(range_enabled)
        self.range_to_edit.setEnabled(range_enabled)
        if searching:
            self._loaded_completed.clear()
            self._loaded_logs.clear()
            self._completed_offset = 0
            self._log_offset = 0
            self._load_search_page(search)
            return
        self.load_more_button.hide()
        logs = list(self._record_service.work_logs(log_date=selected_date))
        completed = self._task_service.completed_on(selected_date)
        self._render_entries(completed, logs, searching=False)

    def _search_dates(self) -> tuple[date | None, date | None]:
        if not self.range_checkbox.isChecked():
            return None, None
        date_from = cast(date, self.range_from_edit.date().toPython())
        date_to = cast(date, self.range_to_edit.date().toPython())
        return date_from, date_to

    def _load_more_search(self) -> None:
        search = self.search_edit.text().strip()
        if search:
            self._load_search_page(search)

    def _load_search_page(self, search: str) -> None:
        date_from, date_to = self._search_dates()
        if date_from is not None and date_to is not None and date_from > date_to:
            self.load_more_button.hide()
            self.list_widget.clear()
            self._selected_group_key = None
            self._selected_task_context = None
            self.open_records_button.setEnabled(False)
            self.result_count_label.setText("검색 결과 0개 업무")
            self.detail.setPlainText("검색 시작일은 종료일보다 늦을 수 없습니다.")
            return
        completed_page = self._task_service.completed_search_page(
            search=search,
            date_from=date_from,
            date_to=date_to,
            offset=self._completed_offset,
            limit=self.SEARCH_BATCH_SIZE,
        )
        log_page = self._record_service.work_log_page(
            search=search,
            date_from=date_from,
            date_to=date_to,
            offset=self._log_offset,
            limit=self.SEARCH_BATCH_SIZE,
        )
        self._loaded_completed.extend(completed_page.items)
        self._loaded_logs.extend(log_page.items)
        self._completed_offset += len(completed_page.items)
        self._log_offset += len(log_page.items)
        self._render_entries(
            self._loaded_completed,
            self._loaded_logs,
            searching=True,
        )
        has_more = completed_page.has_more or log_page.has_more
        scope = (
            f"{format_date(date_from)}~{format_date(date_to)}"
            if date_from is not None and date_to is not None
            else "전체 기간"
        )
        self.load_more_button.setVisible(has_more)
        self.load_more_button.setText(f"업무 더 보기 (현재 {len(self._groups_by_key):,}개)")
        if has_more:
            self.result_count_label.setText(
                f"{scope} 검색 결과 현재 {len(self._groups_by_key):,}개 업무 · 더 있음"
            )
        else:
            self.result_count_label.setText(
                f"{scope} 검색 결과 {len(self._groups_by_key):,}개 업무"
            )

    def _render_entries(
        self,
        completed: list[Task],
        logs: list[WorkLog],
        *,
        searching: bool,
    ) -> None:
        selected_key = self._selected_group_key
        task_ids = {task.id for task in completed if task.id is not None} | {
            log.task_id for log in logs if log.task_id is not None
        }
        tasks_by_id = self._task_service.get_many_including_deleted(tuple(task_ids))
        occurrences = self._task_service.occurrences_by_ids(
            tuple({log.occurrence_id for log in logs if log.occurrence_id is not None})
        )
        groups: dict[tuple[str, int], _RecordGroup] = {}
        for completed_task in completed:
            if completed_task.id is None:
                continue
            key = (
                ("occurrence", completed_task.occurrence_id)
                if completed_task.occurrence_id is not None
                else ("task", completed_task.id)
            )
            group = groups.setdefault(
                key,
                _RecordGroup(
                    key=key,
                    task=tasks_by_id.get(completed_task.id, completed_task),
                ),
            )
            group.completed = completed_task
            group.summary = completed_task.result_note
            group.occurrence_start = completed_task.occurrence_start
        for log in logs:
            if log.task_id is None:
                if log.id is None:
                    continue
                key = ("log", log.id)
            else:
                key = (
                    ("occurrence", log.occurrence_id)
                    if log.occurrence_id in occurrences
                    else ("task", log.task_id)
                )
            group = groups.setdefault(
                key,
                _RecordGroup(
                    key=key,
                    task=(tasks_by_id.get(log.task_id) if log.task_id is not None else None),
                ),
            )
            group.logs.append(log)
            if group.completed is None:
                occurrence = (
                    occurrences.get(log.occurrence_id) if log.occurrence_id is not None else None
                )
                if occurrence is not None:
                    group.occurrence_start = occurrence.occurrence_start
                    group.summary = occurrence.result_note
                elif group.task is not None:
                    group.summary = group.task.result_note

        search = self.search_edit.text().strip()
        self._filename_matches = (
            self._attachment_service.filename_matches_for_tasks(tuple(task_ids), search)
            if self._attachment_service is not None
            else {}
        )
        for group in groups.values():
            group.match_locations = self._match_locations(group, search)

        task_group_ids = tuple(key[1] for key in groups if key[0] == "task")
        work_log_counts = self._record_service.work_log_counts(task_group_ids)
        occurrence_counts = self._record_service.occurrence_work_log_counts(
            tuple(key[1] for key in groups if key[0] == "occurrence")
        )
        ordered_groups = sorted(
            groups.values(),
            key=self._group_sort_key,
            reverse=True,
        )
        self._groups_by_key = groups
        self._selected_task_context = None
        self.open_records_button.setEnabled(False)
        self.list_widget.clear()
        selected_row = -1
        for row, group in enumerate(ordered_groups):
            title = group.task.title if group.task is not None else "연결되지 않은 기록"
            if group.occurrence_start is not None and group.task is not None:
                local_start = group.occurrence_start.astimezone(ZoneInfo(group.task.timezone))
                title += f" · {format_datetime(local_start, group.task.timezone)} 회차"
            states: list[str] = []
            if group.completed is not None:
                states.append("완료")
            if group.key[0] == "task":
                count = work_log_counts.get(group.key[1], 0)
            elif group.key[0] == "occurrence":
                count = occurrence_counts.get(group.key[1], 0)
            else:
                count = len(group.logs)
            group.work_log_count = count
            states.append(f"업무일지 {count:,}건")
            if searching and group.match_locations:
                states.append("검색 위치: " + "·".join(sorted(group.match_locations)))
            item = QListWidgetItem(f"{title}  ·  {'  ·  '.join(states)}")
            item.setData(Qt.ItemDataRole.UserRole, ("group", *group.key))
            self.list_widget.addItem(item)
            if group.key == selected_key:
                selected_row = row
        noun = "항목" if any(key[0] == "occurrence" for key in groups) else "업무"
        self.result_count_label.setText(
            f"검색 결과 {len(groups):,}개 {noun}"
            if searching
            else f"이 날짜의 기록 {len(groups):,}개 {noun}"
        )
        if selected_row >= 0:
            self.list_widget.setCurrentRow(selected_row)
        else:
            self._selected_group_key = None
            self.detail.setPlainText(
                (
                    "검색 결과가 없습니다. 제목, 설명, 완료 요약, 업무일지 내용 또는 첨부파일명으로 검색해 보세요."
                    if searching
                    else "이 날짜에 완료한 업무나 작성한 업무일지가 없습니다."
                )
                if not groups
                else "업무를 선택하면 완료 요약, 업무일지와 첨부파일을 함께 확인할 수 있습니다."
            )

    def _match_locations(self, group: _RecordGroup, search: str) -> set[str]:
        terms = tuple(term.casefold() for term in search.split() if term)
        if not terms:
            return set()

        def contains_term(value: str) -> bool:
            normalized = value.casefold()
            return any(term in normalized for term in terms)

        locations: set[str] = set()
        task = group.task
        if task is not None:
            if task.id in self._filename_matches:
                locations.add("첨부파일명")
            if contains_term(task.title):
                locations.add("제목")
            if contains_term(task.description):
                locations.add("설명")
            if contains_term(group.summary):
                locations.add("완료 요약")
        for log in group.logs:
            if contains_term(log.content):
                locations.add("업무일지")
            if contains_term(log.result):
                locations.add("진행 결과")
        return locations

    @staticmethod
    def _group_sort_key(group: _RecordGroup) -> tuple[int, str]:
        dates = [log.log_date.toordinal() for log in group.logs]
        if group.completed is not None and group.completed.completed_at is not None:
            zone = ZoneInfo(group.completed.timezone)
            dates.append(group.completed.completed_at.astimezone(zone).date().toordinal())
        title = group.task.title.casefold() if group.task is not None else ""
        return max(dates, default=0), title

    def _show_group_detail(self, group: _RecordGroup) -> None:
        task = group.task
        if task is None or task.id is None:
            logs = group.logs
            self._selected_task_context = None
            title = "연결되지 않은 기록"
            summary = ""
            status = "업무 연결 없음"
            attachment_text = "첨부파일 0개"
            work_log_count = len(logs)
        else:
            occurrence_start = group.occurrence_start
            if occurrence_start is None:
                for log in sorted(
                    group.logs,
                    key=lambda item: (item.log_date, item.updated_at),
                    reverse=True,
                ):
                    if log.occurrence_id is None:
                        continue
                    occurrence = self._task_service.occurrence_by_id(log.occurrence_id)
                    if occurrence is not None and occurrence.task_id == task.id:
                        occurrence_start = occurrence.occurrence_start
                        break
            self._selected_task_context = (task.id, occurrence_start)
            title = task.title
            summary = self._task_service.result_note(task.id, occurrence_start)
            status = "완료" if group.completed is not None else "업무 기록"
            logs = list(
                self._record_service.work_logs(
                    task_id=task.id,
                    limit=20,
                    occurrence_start=occurrence_start,
                    occurrence_only=occurrence_start is not None,
                )
            )
            work_log_count = group.work_log_count
            if self._attachment_service is not None:
                attachments = self._attachment_service.attachments_for_task(task.id)
                attachment_text = f"첨부파일 {len(attachments):,}개"
            else:
                attachment_text = "첨부파일 있음" if task.has_attachments else "첨부파일 0개"

        lines = [f"{title} · {status}"]
        if group.match_locations:
            lines.extend(("", "검색 위치: " + " · ".join(sorted(group.match_locations))))
        lines.extend(("", "완료 요약", summary or "입력된 완료 요약이 없습니다."))
        log_heading = f"업무일지 {work_log_count:,}건"
        if work_log_count > len(logs):
            log_heading += f" · 최근 {len(logs):,}건 표시"
        lines.extend(("", log_heading))
        if not logs:
            lines.append("작성된 업무일지가 없습니다.")
        for log in logs:
            content = " ".join(log.content.splitlines())
            lines.append(f"- {format_date(log.log_date)}  {content}")
            if log.result:
                result = " ".join(log.result.splitlines())
                lines.append(f"  진행 결과 / 다음 단계: {result}")
        lines.extend(("", attachment_text))
        self.detail.setPlainText("\n".join(lines))

    def _show_selected(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        if current is None:
            self._selected_group_key = None
            self._selected_task_context = None
            self.open_records_button.setEnabled(False)
            return
        data = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, tuple) or len(data) != 3 or data[0] != "group":
            return
        key = (str(data[1]), int(data[2]))
        group = self._groups_by_key.get(key)
        if group is None:
            self._selected_group_key = None
            self._selected_task_context = None
            self.open_records_button.setEnabled(False)
            return
        self._selected_group_key = key
        self._show_group_detail(group)
        self.open_records_button.setEnabled(self._selected_task_context is not None)

    def _open_selected_records(self) -> None:
        if self._selected_task_context is None:
            return
        task_id, occurrence_start = self._selected_task_context
        for existing in self._record_dialogs:
            if existing._task_id == task_id and existing._occurrence_start == occurrence_start:
                existing.show()
                existing.raise_()
                existing.activateWindow()
                return
        try:
            task = self._task_service.get(task_id)
        except LookupError:
            return
        dialog = TaskRecordsDialog(
            task,
            task_service=self._task_service,
            record_service=self._record_service,
            attachment_service=self._attachment_service,
            occurrence_start=occurrence_start,
            attachment_export_service=self._attachment_export_service,
            attachment_export_directory=self._attachment_export_directory,
            initial_tab="result",
            folder_link_service=self._folder_link_service,
            image_coordinator=self._image_coordinator,
            initial_window_size=self._record_window_size() if self._record_window_size else None,
            parent=self,
        )
        self._record_dialogs.append(dialog)
        dialog.exportDirectoryChanged.connect(self.exportDirectoryChanged)
        dialog.windowSizeChanged.connect(self.windowSizeChanged)
        dialog.changed.connect(self._refresh)
        dialog.changed.connect(self.changed.emit)

        def finished(_result: int) -> None:
            if dialog in self._record_dialogs:
                self._record_dialogs.remove(dialog)
            dialog.deleteLater()
            if self.isVisible():
                self._refresh()

        dialog.finished.connect(finished)
        dialog.show()

    def done(self, result: int) -> None:
        self._search_timer.stop()
        for dialog in tuple(self._record_dialogs):
            dialog.shutdown()
        if self._owns_image_coordinator:
            self._image_coordinator.shutdown()
        super().done(result)

    def shutdown(self) -> None:
        """Stop nested file imports before the main window releases its database."""
        self.reject()
