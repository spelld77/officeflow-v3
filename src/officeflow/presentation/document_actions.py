"""Modeless document workflows. Slow file work never runs on the Qt thread."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, QPoint, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from officeflow.application.attachment_deletion import DeletePlan, DeleteTarget
from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.attachment_export import AttachmentExportService, FileReference
from officeflow.application.attachments import AttachmentOperationError
from officeflow.application.document_lifecycle import DocumentLifecycle
from officeflow.application.file_operations import FileInUseError
from officeflow.infrastructure.attachments.view_cache import ViewCopy
from officeflow.infrastructure.database.document_catalog import DocumentEntry
from officeflow.presentation.attachment_export import AttachmentExportController
from officeflow.presentation.background import OperationWorker, finish_thread
from officeflow.presentation.compact_labels import CompactLabel
from officeflow.presentation.dates import format_datetime
from officeflow.presentation.file_list import compact_file_list, set_file_row
from officeflow.presentation.task_history_dialog import _ReadDialog
from officeflow.presentation.theme import LIGHT_STYLESHEET


def after_change(changed: Callable[[], None], dialog: QDialog) -> Callable[[Any], None]:
    def finish(_value: object) -> None:
        changed()
        dialog.accept()

    return finish


class FileActionController(QObject):
    busyChanged = Signal(bool)

    def __init__(self, parent: QWidget, lifecycle: DocumentLifecycle) -> None:
        super().__init__(parent)
        self.owner, self.lifecycle = parent, lifecycle
        self._job_thread: QThread | None = None
        self.worker: OperationWorker | None = None
        self.dialogs: list[QDialog] = []
        self._retired: list[QDialog] = []
        self.closed = False
        self._success: Callable[[Any], None] = lambda _value: None
        self._status: QLabel | None = None
        self._feedback: QLabel | None = None
        self._button: QPushButton | None = None
        self._progress_dialog: QDialog | None = None
        self._close_on_success = False
        self._succeeded_cleanly = False

    def keep(self, dialog: QDialog) -> None:
        dialog.setStyleSheet(LIGHT_STYLESHEET)
        dialog.setModal(False)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        self.dialogs.append(dialog)

        def released(_code: int) -> None:
            if dialog in self.dialogs:
                self.dialogs.remove(dialog)
            if self._job_thread:
                self._retired.append(dialog)
            else:
                dialog.deleteLater()

        dialog.finished.connect(released)
        dialog.show()

    def run(
        self,
        label: str,
        operation: Callable[[Callable[[], bool]], object],
        success: Callable[[Any], None] = lambda _value: None,
        *,
        feedback: QLabel | None = None,
        close_on_success: bool = False,
    ) -> None:
        if self.closed or self._job_thread is not None:
            return
        dialog = QDialog(self.owner)
        dialog.setWindowTitle(label)
        layout = QVBoxLayout(dialog)
        status = QLabel(label + "…\n다른 업무와 알림은 계속 사용할 수 있습니다.")
        status.setWordWrap(True)
        layout.addWidget(status)
        button = QPushButton("취소 요청")
        layout.addWidget(button)
        self.keep(dialog)
        thread = QThread(self)
        worker = OperationWorker(operation)
        worker.moveToThread(thread)
        button.clicked.connect(worker.cancel)
        # Reject/close only requests cancellation, never destroys the worker.
        dialog.rejected.connect(worker.cancel)

        self._success, self._status, self._feedback, self._button, self._progress_dialog = (
            success,
            status,
            feedback,
            button,
            dialog,
        )
        self._close_on_success = close_on_success
        self._succeeded_cleanly = False
        worker.succeeded.connect(self._succeeded, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.finished.connect(worker.deleteLater)

        thread.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        thread.started.connect(worker.run)
        self._job_thread, self.worker = thread, worker
        self.busyChanged.emit(True)
        thread.start()

    @Slot(object)
    def _succeeded(self, value: object) -> None:
        if self.closed:
            return
        try:
            self._success(value)
            warning = getattr(value, "warning", "")
            message = warning or (value if isinstance(value, str) else "완료했습니다.")
            self._succeeded_cleanly = not bool(warning)
        except Exception as error:
            self._succeeded_cleanly = False
            message = str(error)
        if self._close_on_success and self._succeeded_cleanly:
            # Opening is the result: do not overwrite file details or ask for dismissal.
            # The progress window is released only after the native worker has joined.
            return
        if self._status and isValid(self._status):
            self._status.setText(message)
        if self._feedback and isValid(self._feedback):
            self._feedback.setText(message)

    @Slot(object)
    def _failed(self, error: object) -> None:
        if self.closed:
            return
        if self._status and isValid(self._status):
            self._status.setText(str(error))
        if self._feedback and isValid(self._feedback):
            self._feedback.setText(str(error))

    @Slot()
    def _finished(self) -> None:
        thread = self._job_thread
        if thread is not None and isValid(thread):
            # finished is emitted before native thread-local destructors finish.
            # Join this completed worker before dropping Python/Qt ownership.
            thread.wait()
        self._job_thread, self.worker = None, None
        if (
            not self.closed
            and self._button
            and isValid(self._button)
            and self._progress_dialog
            and isValid(self._progress_dialog)
            and self._progress_dialog not in self._retired
        ):
            if self._close_on_success and self._succeeded_cleanly:
                self._progress_dialog.accept()
            else:
                self._button.setText("닫기")
                self._button.clicked.connect(self._progress_dialog.accept)
        for dialog in self._retired:
            if isValid(dialog):
                dialog.deleteLater()
        self._retired.clear()
        self._success = lambda _value: None
        self._feedback = None
        self._status, self._button, self._progress_dialog = None, None, None
        self._close_on_success = False
        self._succeeded_cleanly = False
        if thread is not None and isValid(thread):
            thread.deleteLater()
        self.busyChanged.emit(False)

    def open(self, kind: str, identity: int, *, feedback: QLabel | None = None) -> None:
        def launch(path: Path) -> None:
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                # Failure doesn't imply a running editor; normal cleanup still verifies bytes.
                copy = self.lifecycle.cache.by_path(path)
                if copy:
                    from dataclasses import replace

                    self.lifecycle.cache._save(replace(copy, closed=True))
                raise OSError("연결 프로그램을 열지 못했습니다. 접근 권한을 확인하세요.")

        self.run(
            "파일 열기",
            lambda canceled: self.lifecycle.prepare_open(kind, identity, canceled),
            launch,
            feedback=feedback,
            close_on_success=True,
        )

    def create(
        self, entry: DocumentEntry, operation: DocumentOperation, changed: Callable[[], None]
    ) -> None:
        if self._job_thread is not None:
            return
        dialog = QDialog(self.owner)
        dialog.setWindowTitle(
            {
                DocumentOperation.WORK: "새 작업본",
                DocumentOperation.VERSION: "보관본 남기기",
                DocumentOperation.ORIGINAL: "원본으로 지정",
                DocumentOperation.RELEASE_ORIGINAL: "원본 지정 해제",
            }[operation]
        )
        dialog.resize(460, 300)
        root = QVBoxLayout(dialog)
        hint = QLabel(
            "현재 저장된 내용을 독립 파일로 캡처합니다. 먼저 문서를 저장하고 닫아 주세요.\n새 파일은 자동으로 열지 않습니다."
        )
        hint.setWordWrap(True)
        root.addWidget(hint)
        form = QFormLayout()
        name, memo = QLineEdit(), QPlainTextEdit()
        memo.setTabChangesFocus(True)
        memo.setMaximumHeight(100)
        if operation is DocumentOperation.WORK:
            name.setText(self.lifecycle.catalog.work_suggestion(entry.task_id, entry.name))
            form.addRow("파일 이름", name)
        elif operation is DocumentOperation.VERSION:
            name.setText(self.lifecycle.catalog.suggestion(entry.family_id))
            form.addRow("보관 이름", name)
            form.addRow("메모 (선택)", memo)
        elif operation is DocumentOperation.ORIGINAL:
            hint.setText(
                "현재 저장된 내용이 고정됩니다. 이전 편집 파일은 첨부 정리의 해제 목록에 보존합니다.\n원본은 보기 사본으로 열립니다. 수정하려면 작업본을 만드세요."
            )
        else:
            hint.setText(
                "이 첨부를 편집 가능한 일반 첨부로 전환합니다.\n"
                "해제 전 원본은 보관함의 '원본 해제 전 내용'으로 보존합니다.\n"
                "보기 사본에서 수정한 내용은 반영되지 않으며, 사본도 지우지 않습니다."
            )
        root.addLayout(form)
        feedback = QLabel("")
        feedback.setWordWrap(True)
        root.addWidget(feedback)
        button = QPushButton(
            {
                DocumentOperation.ORIGINAL: "원본 지정",
                DocumentOperation.RELEASE_ORIGINAL: "원본 지정 해제",
            }.get(operation, "생성")
        )
        root.addWidget(button)
        job_id = uuid4().hex
        approved: tuple[str, str, str] | None = None

        def created(value: object) -> None:
            changed()
            identity = getattr(value, "attachment_id", None)
            select = getattr(self.owner, "select_attachment", None)
            if identity is not None and callable(select):
                select(identity)
            dialog.accept()

        def submit() -> None:
            nonlocal approved
            filename = name.text() if operation is DocumentOperation.WORK else ""
            label = name.text() if operation is DocumentOperation.VERSION else ""
            text = memo.toPlainText() if operation is DocumentOperation.VERSION else ""
            values = filename, label, text
            if approved is not None and approved != values:
                feedback.setText(
                    "중단 요청의 입력을 유지해 재시도하세요. 내용을 바꾸려면 이 창을 닫고 새 명령으로 시작하세요."
                )
                return
            approved = values
            self.run(
                "파일 캡처",
                lambda canceled: self.lifecycle.create(
                    entry,
                    operation,
                    filename=filename,
                    label=label,
                    memo=text,
                    job_id=job_id,
                    canceled=canceled,
                ),
                created,
                feedback=feedback,
            )

        button.clicked.connect(submit)
        self.keep(dialog)

    def delete(self, kind: str, identity: int, changed: Callable[[], None]) -> None:
        self.run(
            "삭제 계획 확인",
            lambda _canceled: self.lifecycle.plan(kind, identity),
            lambda plan: self._confirm_delete(plan, changed),
            close_on_success=True,
        )

    def delete_many(
        self, task_id: int, targets: tuple[DeleteTarget, ...], changed: Callable[[], None]
    ) -> None:
        if not targets:
            return
        if len(targets) == 1:
            self.delete(targets[0].kind, targets[0].id, changed)
            return

        def planned(_canceled: Callable[[], bool]) -> tuple[DeletePlan, tuple[str, ...]]:
            plan = self.lifecycle.plan_many(task_id, targets)
            names = []
            for item in targets:
                entry = self.lifecycle.catalog.get(item.kind, item.id)
                role = (
                    "보관본"
                    if entry.kind == "version"
                    else {"general": "일반", "original": "원본", "work": "작업본"}[
                        entry.file.role.value
                    ]
                )
                names.append(
                    f"[{role}] " + (entry.label + " · " if entry.label else "") + entry.name
                )
            return plan, tuple(names)

        self.run(
            "선택 파일 삭제 계획 확인",
            planned,
            lambda result: self._confirm_delete(result[0], changed, result[1]),
            close_on_success=True,
        )

    def _confirm_delete(
        self, plan: DeletePlan, changed: Callable[[], None], names: tuple[str, ...] = ()
    ) -> None:
        kind = plan.target.kind
        dialog = QDialog(self.owner)
        dialog.setObjectName("fileDeleteConfirmation")
        dialog.setWindowTitle("영구 삭제 범위 확인")
        dialog.resize(480, 360 if names else 280)
        root = QVBoxLayout(dialog)
        if names:
            listing = QPlainTextEdit("\n".join(names))
            listing.setReadOnly(True)
            listing.setObjectName("fileDeleteTargetNames")
            root.addWidget(listing, 1)
        text = QLabel(
            f"연결 {plan.attachment_count}개 · 보관본 {plan.version_count}개\n업무 이력 {plan.history_count}건 · 중요 보관 {plan.snapshot_count}개\n실제 파일 {plan.removable_count}개 삭제 / 다른 참조 파일 {plan.kept_asset_count}개 유지\n남는 보관본 {plan.kept_versions}개\n외부 사본과 기존 백업은 삭제하지 않습니다. 되돌릴 수 없습니다."
        )
        text.setWordWrap(True)
        root.addWidget(text)
        scope = QCheckBox("보관본·업무 이력·중요 보관도 함께 삭제합니다")
        scope.setVisible(
            kind == "task" and bool(plan.version_count or plan.history_count or plan.snapshot_count)
        )
        root.addWidget(scope)
        consent = QCheckBox("삭제 범위를 확인했습니다")
        consent.setObjectName("fileDeleteConsent")
        root.addWidget(consent)
        button = QPushButton("영구 삭제")
        button.setObjectName("confirmFileDeleteButton")
        button.setEnabled(False)
        actions = QHBoxLayout()
        actions.addWidget(button)
        cancel = QPushButton("취소")
        cancel.clicked.connect(dialog.reject)
        actions.addWidget(cancel)
        root.addLayout(actions)

        def enable() -> None:
            button.setEnabled(consent.isChecked() and (not scope.isVisible() or scope.isChecked()))

        consent.toggled.connect(enable)
        scope.toggled.connect(enable)

        def submit() -> None:
            include = kind == "task" and scope.isChecked()
            self.run(
                "영구 삭제",
                lambda canceled: self.lifecycle.delete(
                    plan,
                    include_versions=include,
                    canceled=canceled,
                ),
                after_change(changed, dialog),
            )

        button.clicked.connect(submit)
        self.keep(dialog)

    def shutdown(self) -> None:
        self.closed = True
        if self.worker and isValid(self.worker):
            self.worker.cancel()
        if self._job_thread and isValid(self._job_thread):
            finish_thread(
                self._job_thread,
                parent=self.owner,
                label="문서 작업을 안전하게 마무리하고 있습니다…",
            )
        for dialog in tuple(self.dialogs):
            if not isValid(dialog):
                continue
            if hasattr(dialog, "shutdown"):
                dialog.shutdown()
            dialog.close()


def file_actions(parent: QWidget, lifecycle: DocumentLifecycle) -> FileActionController:
    result = parent.property("documentActionsController")
    if not isinstance(result, FileActionController):
        result = FileActionController(parent, lifecycle)
        parent.setProperty("documentActionsController", result)
    return result


def stop_file_actions(parent: QWidget) -> None:
    result = parent.property("documentActionsController")
    if isinstance(result, FileActionController):
        result.shutdown()


class DocumentInformationDialog(_ReadDialog):
    def __init__(
        self, lifecycle: DocumentLifecycle, kind: str, identity: int, timezone: str, parent: QWidget
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("파일 정보")
        self.setObjectName("documentInformationDialog")
        self.resize(520, 420)
        root = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        root.addWidget(self.text, 1)
        root.addWidget(self.feedback)
        close = QPushButton("닫기")
        close.clicked.connect(self.reject)
        root.addWidget(close)
        self.run(
            lambda: lifecycle.catalog.get(kind, identity),
            lambda row: self._ready_entry(row, timezone),
        )

    def _ready_entry(self, row: DocumentEntry, timezone: str) -> None:
        source = row.current_source_name or (
            "출처 항목 삭제됨" if row.source_name_at_creation else "직접 첨부"
        )
        lines = [
            f"파일 이름: {row.name}",
            f"종류: {document_role(row)}",
            f"최초 파일 이름: {row.file.original_name}",
            f"생성/보관 시각: {format_datetime(row.file.created_at, timezone)}",
            f"보관 이름: {row.label or '없음'}",
            f"생성 당시 출처: {row.source_name_at_creation or '직접 첨부'}",
            f"현재 출처: {source}",
            f"메모:\n{row.memo or '없음'}",
        ]
        self.text.setPlainText("\n\n".join(lines))


def document_role(row: DocumentEntry) -> str:
    return (
        "보관본"
        if row.kind == "version"
        else {"general": "일반 첨부", "original": "원본", "work": "작업본"}[row.file.role.value]
    )


class DocumentBrowser(_ReadDialog):
    """One task, bounded keyset pages and ID-based selections across pages."""

    def __init__(
        self,
        lifecycle: DocumentLifecycle,
        exports: AttachmentExportService,
        task_id: int,
        *,
        family_id: str | None = None,
        read_only: bool = False,
        timezone: str = "Asia/Seoul",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.lifecycle, self.task_id, self.family_id = lifecycle, task_id, family_id
        self.read_only = read_only
        self.timezone = timezone
        self.setStyleSheet(LIGHT_STYLESHEET)
        self.setWindowTitle("보관함")
        self.resize(640, 480)
        self.root = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.kind = QComboBox()
        self.kind.addItems(["보관본", "현재 첨부"])
        self.all_families = QCheckBox("이 업무 전체 보기")
        self.all_families.setChecked(family_id is None)
        self.all_families.setVisible(family_id is not None)
        controls.addWidget(self.kind)
        controls.addWidget(self.all_families)
        self.root.addLayout(controls)
        self.list = QListWidget()
        self.list.setObjectName("documentFileList")
        compact_file_list(self.list)
        self.list.itemChanged.connect(self._checked)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.itemDoubleClicked.connect(lambda _item: self.open())
        self.list.currentItemChanged.connect(self._detail)
        self.list.itemSelectionChanged.connect(self._update_selection)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.root.addWidget(self.list, 1)
        self.selection_status = CompactLabel()
        self.summary = CompactLabel()
        self.root.addWidget(self.selection_status)
        self.root.addWidget(self.summary)
        self.feedback = QLabel("")
        self.feedback.setWordWrap(True)
        self.root.addWidget(self.feedback)
        self.navigation = QWidget(self)
        navigation = QHBoxLayout(self.navigation)
        navigation.setContentsMargins(0, 0, 0, 0)
        self.previous_button = QPushButton("이전 페이지")
        self.previous_button.setObjectName("documentPreviousPageButton")
        self.previous_button.clicked.connect(self.previous)
        self.page_label = QLabel("")
        self.next_button = QPushButton("다음 페이지")
        self.next_button.setObjectName("documentNextPageButton")
        self.next_button.clicked.connect(self.next)
        navigation.addWidget(self.previous_button)
        navigation.addWidget(self.page_label)
        navigation.addStretch()
        navigation.addWidget(self.next_button)
        self.root.addWidget(self.navigation)
        buttons = QHBoxLayout()
        self.open_button = QPushButton("열기")
        self.open_button.clicked.connect(self.open)
        self.save_button = QPushButton("체크한 파일 저장…")
        self.save_button.clicked.connect(self.save)
        self.more_button = QPushButton("더보기 ▾")
        self.more_button.clicked.connect(self._more_menu)
        for button in (self.open_button, self.save_button, self.more_button):
            buttons.addWidget(button)
        self.root.addLayout(buttons)
        self.exporter = AttachmentExportController(exports, self)
        self.exporter.busyChanged.connect(self._update_selection)
        file_actions(self, self.lifecycle).busyChanged.connect(self._update_selection)
        self.selected: set[tuple[str, int]] = set()
        self.entries: dict[tuple[str, int], DocumentEntry] = {}
        self.cursors: list[int | None] = [None]
        self.has_next = False
        self._summary_key: tuple[str, int] | None = None
        self._resolved_summary_key: tuple[str, int] | None = None
        self.kind.currentIndexChanged.connect(self.reset)
        self.all_families.toggled.connect(self.reset)
        self.refresh()

    def reset(self) -> None:
        self.cursors = [None]
        self.refresh()

    def refresh(self) -> None:
        highlighted = {item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems()}
        current = self.list.currentItem()
        current_key = current.data(Qt.ItemDataRole.UserRole) if current else None
        scroll = self.list.verticalScrollBar().value()
        kind = "version" if self.kind.currentIndex() == 0 else "attachment"
        while True:
            page = self.lifecycle.catalog.page_with_more(
                self.task_id,
                kind=kind,
                family_id=None if self.all_families.isChecked() else self.family_id,
                before=self.cursors[-1],
                limit=50 if kind == "version" else 100,
            )
            if page.items or len(self.cursors) == 1:
                break
            self.cursors.pop()
        rows = page.items
        self.has_next = page.has_next
        self.navigation.setVisible(len(self.cursors) > 1 or self.has_next)
        self.previous_button.setEnabled(len(self.cursors) > 1)
        self.next_button.setEnabled(self.has_next)
        self.page_label.setText(f"{len(self.cursors)}페이지")
        self.entries = {(row.kind, row.id): row for row in rows}
        self.list.blockSignals(True)
        self.list.clear()
        for row in rows:
            item = QListWidgetItem()
            role = (
                "보관본"
                if row.kind == "version"
                else {"general": "일반", "original": "원본", "work": "작업본"}[row.file.role.value]
            )
            display = (row.label + " · " if row.label else "") + row.name
            set_file_row(
                item,
                display,
                role,
                "메모 있음" if row.memo else "",
                tooltip=f"{display}\n{format_datetime(row.file.created_at, self.timezone)}\n{row.memo}",
            )
            key = row.kind, row.id
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if key in self.selected else Qt.CheckState.Unchecked
            )
            self.list.addItem(item)
            if key == current_key:
                self.list.setCurrentItem(item)
            if key in highlighted:
                item.setSelected(True)
        self.list.blockSignals(False)
        self.list.verticalScrollBar().setValue(scroll)
        self._summary_key = None
        self._resolved_summary_key = None
        self._update_selection()

    def check_page(self, checked: bool) -> None:
        for index in range(self.list.count()):
            self.list.item(index).setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )

    def _checked(self, item: QListWidgetItem) -> None:
        key = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.selected.add(key)
        else:
            self.selected.discard(key)
        self._update_selection()

    def current(self) -> DocumentEntry | None:
        items = self.list.selectedItems()
        return (
            self.entries.get(items[0].data(Qt.ItemDataRole.UserRole)) if len(items) == 1 else None
        )

    def _detail(self, _current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        self._update_selection()

    def _busy(self) -> bool:
        return file_actions(self, self.lifecycle)._job_thread is not None or self.exporter.busy

    def _update_selection(self) -> None:
        if not hasattr(self, "entries"):
            return
        items = self.list.selectedItems()
        outside = bool(self.selected.difference(self.entries))
        self.selection_status.setText(
            f"현재 페이지 선택 {len(items)}개 · 저장 체크 {len(self.selected)}개"
            + (" (다른 페이지 포함)" if outside else "")
        )
        row = self.current()
        busy = self._busy()
        self.open_button.setEnabled(row is not None and not busy)
        self.save_button.setEnabled(bool(self.selected) and not busy)
        self.summary.setVisible(bool(items))
        if row is None:
            self._summary_key = None
            self._generation += 1
            self._pending = None
            self.summary.setText(f"선택 {len(items)}개" if items else "")
            return
        key = row.kind, row.id
        self._show_summary(row, resolved=key == self._resolved_summary_key)
        if key != self._summary_key:
            self._summary_key = key
            self.run(lambda: self.lifecycle.catalog.get(*key), self._summary_ready)

    def _show_summary(self, row: DocumentEntry, *, resolved: bool = False) -> None:
        text = f"{document_role(row)} · {format_datetime(row.file.created_at, self.timezone, short=True)}"
        if row.memo:
            text += " · 메모 있음"
        if row.source_name_at_creation:
            if not resolved:
                text += "\n출처 확인 중"
            elif not row.current_source_name:
                text += "\n출처 항목 삭제됨"
            elif (
                row.current_source_file_name
                and row.current_source_file_name != row.source_name_at_creation
            ):
                text += "\n출처 이름 변경됨"
        self.summary.setText(text)

    def _summary_ready(self, row: DocumentEntry) -> None:
        current = self.current()
        if current is not None and (current.kind, current.id) == (row.kind, row.id):
            self.entries[row.kind, row.id] = row
            self._resolved_summary_key = row.kind, row.id
            self._show_summary(row, resolved=True)

    def information(self) -> None:
        row = self.current()
        if row is not None and not self._busy():
            file_actions(self, self.lifecycle).keep(
                DocumentInformationDialog(self.lifecycle, row.kind, row.id, self.timezone, self)
            )

    def clear_checks(self) -> None:
        self.selected.clear()
        self.check_page(False)
        self._update_selection()

    def _build_menu(self, *, row_actions: bool = True) -> QMenu:
        menu = QMenu(self)
        menu.setStyleSheet(LIGHT_STYLESHEET)
        row = self.current() if row_actions else None
        busy = self._busy()
        for label, callback, enabled in (
            ("파일 정보…", self.information, row is not None),
            ("작업본 만들기…", self.work, row is not None and not self.read_only),
            (
                "메모 수정…",
                self.memo,
                row is not None and row.kind == "version" and not self.read_only,
            ),
        ):
            action = menu.addAction(label)
            action.setEnabled(enabled and not busy)
            action.triggered.connect(callback)
        menu.addSeparator()
        for label, callback in (
            ("이 페이지 저장 체크", lambda: self.check_page(True)),
            ("이 페이지 저장 체크 해제", lambda: self.check_page(False)),
            ("저장 체크 모두 해제", self.clear_checks),
        ):
            action = menu.addAction(label)
            action.setEnabled(not busy)
            action.triggered.connect(callback)
        menu.addSeparator()
        count = len(self.list.selectedItems()) if row_actions else 0
        delete = menu.addAction(f"선택 파일 {count}개 삭제…")
        delete.setObjectName("documentDeleteSelectionAction")
        delete.setToolTip(
            "강조 선택한 현재 페이지 파일만 삭제합니다. 저장 체크는 삭제 대상이 아닙니다."
        )
        delete.setEnabled(not busy and not self.read_only and 1 <= count <= 100)
        delete.triggered.connect(self.delete)
        return menu

    def _more_menu(self) -> None:
        menu = self._build_menu()
        menu.exec(self.more_button.mapToGlobal(QPoint(0, self.more_button.height())))
        menu.deleteLater()

    def _context_menu(self, position: QPoint) -> None:
        item = self.list.itemAt(position)
        if item is not None and not item.isSelected():
            self.list.setCurrentItem(item)
        menu = self._build_menu(row_actions=item is not None)
        menu.exec(self.list.viewport().mapToGlobal(position))
        menu.deleteLater()

    def select_attachment(self, identity: int) -> None:
        self.kind.setCurrentIndex(1)
        self.cursors = [None]
        self.refresh()
        for index in range(self.list.count()):
            if self.list.item(index).data(Qt.ItemDataRole.UserRole) == ("attachment", identity):
                self.list.setCurrentRow(index)
                break

    def focus_version(self, identity: int) -> None:
        """Jump to a search hit without loading earlier pages into the UI."""
        entry = self.lifecycle.catalog.get("version", identity)
        if entry.task_id != self.task_id:
            raise ValueError("다른 업무의 보관본입니다.")
        self.kind.setCurrentIndex(0)
        self.cursors = [None, identity + 1]
        self.refresh()
        for index in range(self.list.count()):
            if self.list.item(index).data(Qt.ItemDataRole.UserRole) == ("version", identity):
                self.list.setCurrentRow(index)
                break

    def next(self) -> None:
        if self.has_next and self.entries:
            self.cursors.append(min(entry.id for entry in self.entries.values()))
            self.refresh()

    def previous(self) -> None:
        if len(self.cursors) > 1:
            self.cursors.pop()
            self.refresh()

    def open(self) -> None:
        row = self.current()
        if row and not self._busy():
            file_actions(self, self.lifecycle).open(row.kind, row.id, feedback=self.feedback)

    def work(self) -> None:
        row = self.current()
        if row and not self.read_only and not self._busy():
            file_actions(self, self.lifecycle).create(row, DocumentOperation.WORK, self.refresh)

    def memo(self) -> None:
        row = self.current()
        if row is None or row.kind != "version" or self.read_only or self._busy():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("보관 메모")
        root = QVBoxLayout(dialog)
        edit = QPlainTextEdit(row.memo)
        edit.setTabChangesFocus(True)
        root.addWidget(edit)
        button = QPushButton("저장")
        root.addWidget(button)

        def submit() -> None:
            text = edit.toPlainText()
            file_actions(self, self.lifecycle).run(
                "메모 저장",
                lambda _canceled: self.lifecycle.catalog.set_version_memo(
                    row.id, row.revision, text
                ),
                after_change(self.refresh, dialog),
                feedback=self.feedback,
            )

        button.clicked.connect(submit)
        file_actions(self, self.lifecycle).keep(dialog)

    def delete(self) -> None:
        if self.read_only or self._busy():
            return
        keys = tuple(item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems())
        if not keys:
            return
        targets = tuple(DeleteTarget(kind, identity) for kind, identity in keys)

        def changed() -> None:
            self.selected.difference_update(keys)
            self.refresh()

        file_actions(self, self.lifecycle).delete_many(self.task_id, targets, changed)

    def save(self) -> None:
        if not self.selected or self._busy():
            return
        references = tuple(
            FileReference.version(identity)
            if kind == "version"
            else FileReference.attachment(identity)
            for kind, identity in sorted(self.selected)
        )
        self.exporter.choose(references)

    def shutdown(self) -> None:
        super().shutdown()
        stop_file_actions(self)
        self.exporter.shutdown()

    def done(self, result: int) -> None:
        self.shutdown()
        super().done(result)


class ViewCacheDialog(QDialog):
    def __init__(
        self,
        lifecycle: DocumentLifecycle,
        parent: QWidget | None = None,
        *,
        exports: AttachmentExportService | None = None,
        asset_id: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.lifecycle = lifecycle
        self.asset_id = asset_id
        self.setStyleSheet(LIGHT_STYLESHEET)
        self.setWindowTitle("임시 사본")
        self.resize(600, 450)
        root = QVBoxLayout(self)
        hint = QLabel("저장된 변경은 작업본으로 남길 수 있습니다.")
        hint.setToolTip(
            "문서의 저장된 내용만 검사합니다. 편집기를 저장하고 닫은 뒤 저장해주세요. 수정본·종료 미확인·이전 복원 사본은 자동 삭제하지 않습니다."
        )
        hint.setWordWrap(True)
        root.addWidget(hint)
        self.list = QListWidget()
        self.list.setObjectName("viewCopyList")
        compact_file_list(self.list)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self._update_actions)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        root.addWidget(self.list, 1)
        self.selection_status = CompactLabel()
        root.addWidget(self.selection_status)
        self.usage_label = CompactLabel()
        root.addWidget(self.usage_label)
        self.feedback = QLabel("")
        self.feedback.setWordWrap(True)
        root.addWidget(self.feedback)
        self.navigation = QWidget(self)
        navigation = QHBoxLayout(self.navigation)
        navigation.setContentsMargins(0, 0, 0, 0)
        self.previous_button = QPushButton("이전 페이지")
        self.previous_button.clicked.connect(self.previous)
        self.page_label = QLabel()
        self.next_button = QPushButton("다음 페이지")
        self.next_button.clicked.connect(self.next)
        for widget in (self.previous_button, self.page_label, self.next_button):
            navigation.addWidget(widget)
        root.addWidget(self.navigation)
        self.offset = 0
        self.has_next = False
        self.exporter = AttachmentExportController(exports, self) if exports else None
        if self.exporter:
            self.exporter.busyChanged.connect(self._update_actions)
        file_actions(self, lifecycle).busyChanged.connect(self._update_actions)
        actions = QHBoxLayout()
        actions.addStretch()
        self.preserve_button = QPushButton("작업본으로 저장…")
        self.preserve_button.clicked.connect(self.preserve)
        self.more_button = QPushButton("더보기 ▾")
        self.more_button.clicked.connect(self._more_menu)
        actions.addWidget(self.preserve_button)
        actions.addWidget(self.more_button)
        root.addLayout(actions)
        self.refresh()

    def _busy(self) -> bool:
        return file_actions(self, self.lifecycle)._job_thread is not None or bool(
            self.exporter and self.exporter.busy
        )

    def _update_actions(self) -> None:
        if not hasattr(self, "preserve_button"):
            return
        copies = self._selected()
        copy = self.current()
        self.selection_status.setText(
            f"선택 {len(copies)}개" + (" · 변경 있음" if copy and copy.state == "modified" else "")
        )
        self.preserve_button.setEnabled(
            bool(
                copy
                and copy.state == "modified"
                and copy.generation == self.lifecycle.cache.generation
                and not self._busy()
            )
        )

    def _build_menu(self, *, row_actions: bool = True) -> QMenu:
        menu = QMenu(self)
        menu.setStyleSheet(LIGHT_STYLESHEET)
        copies = self._selected() if row_actions else ()
        single = len(copies) == 1
        available = not self._busy()
        for label, callback, enabled in (
            ("내용 다시 확인", self.check, single),
            ("문서 닫음 확인…", self.confirm_selected, 1 <= len(copies) <= 100),
            ("사본 외부 저장…", self.save, single and self.exporter is not None),
        ):
            action = menu.addAction(label)
            action.setEnabled(enabled and available)
            action.triggered.connect(callback)
        menu.addSeparator()
        for label, callback, enabled in (
            ("선택 사본 버리기…", self.discard, single),
            ("선택 미수정 사본 정리…", self.cleanup_selected, 1 <= len(copies) <= 100),
        ):
            action = menu.addAction(label)
            action.setEnabled(enabled and available)
            action.triggered.connect(callback)
        return menu

    def _more_menu(self) -> None:
        menu = self._build_menu()
        menu.exec(self.more_button.mapToGlobal(QPoint(0, self.more_button.height())))
        menu.deleteLater()

    def _context_menu(self, position: QPoint) -> None:
        item = self.list.itemAt(position)
        if item is not None and not item.isSelected():
            self.list.setCurrentItem(item)
        menu = self._build_menu(row_actions=item is not None)
        menu.exec(self.list.viewport().mapToGlobal(position))
        menu.deleteLater()

    def save(self) -> None:
        copy = self.current()
        if copy and self.exporter and not self._busy():
            self.exporter.choose((FileReference.cache(copy.relative_path),))

    def _selected(self) -> tuple[ViewCopy, ...]:
        return tuple(item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems())

    def confirm_selected(self) -> None:
        copies = self._selected()
        if self._busy() or not 1 <= len(copies) <= 100:
            return
        dialog = QDialog(self)
        dialog.setObjectName("viewClosedConfirmation")
        dialog.setWindowTitle("문서 닫음 확인")
        dialog.resize(440, 300)
        root = QVBoxLayout(dialog)
        label = QLabel(f"선택 {len(copies)}개 문서를 편집기에서 저장하고 닫았는지 확인하세요.")
        label.setWordWrap(True)
        root.addWidget(label)
        names = QPlainTextEdit("\n".join(copy.name for copy in copies))
        names.setReadOnly(True)
        root.addWidget(names, 1)
        consent = QCheckBox("선택 문서를 저장하고 닫았습니다.")
        root.addWidget(consent)
        button = QPushButton("확인")
        button.setEnabled(False)
        consent.toggled.connect(button.setEnabled)
        root.addWidget(button)

        def confirm(canceled: Callable[[], bool]) -> str:
            done, failed = 0, []
            for copy in copies:
                if canceled():
                    break
                try:
                    self.lifecycle.cache.confirm_closed(copy, copy.use_token)
                    done += 1
                except (OSError, ValueError, AttachmentOperationError, FileInUseError) as error:
                    failed.append(f"{copy.name}: {error}")
            return (
                f"닫음 확인 {done}개 · 실패 {len(failed)}개 · 미처리 {len(copies) - done - len(failed)}개"
                + ("\n" + "\n".join(failed) if failed else "")
            )

        def submit() -> None:
            if consent.isChecked() and not self._busy():
                file_actions(self, self.lifecycle).run(
                    "문서 닫음 확인",
                    confirm,
                    after_change(self.refresh, dialog),
                    feedback=self.feedback,
                )

        button.clicked.connect(submit)
        file_actions(self, self.lifecycle).keep(dialog)

    def cleanup_selected(self) -> None:
        copies = self._selected()
        if self._busy() or not 1 <= len(copies) <= 100:
            return

        def cleanup(canceled: Callable[[], bool]) -> str:
            done, skipped, failed = 0, 0, []
            for copy in copies:
                if canceled():
                    break
                try:
                    current = self.lifecycle.cache.get(copy.generation, copy.cache_id)
                    if (
                        not current.closed
                        or current.state != "normal"
                        or current.generation != self.lifecycle.cache.generation
                        or current.use_token != copy.use_token
                    ):
                        skipped += 1
                        continue
                    with self.lifecycle.documents.coordinator.job(canceled):
                        observed = self.lifecycle.cache.observe(current, canceled)
                        if observed.state != "normal":
                            skipped += 1
                            continue
                        self.lifecycle.cache.discard(observed)
                        done += 1
                except (OSError, ValueError, AttachmentOperationError, FileInUseError) as error:
                    failed.append(f"{copy.name}: {error}")
            return (
                f"정리 {done}개 · 보호/보류 {skipped}개 · 실패 {len(failed)}개 · 미처리 {len(copies) - done - skipped - len(failed)}개"
                + ("\n" + "\n".join(failed) if failed else "")
            )

        file_actions(self, self.lifecycle).run(
            "선택 미수정 사본 정리", cleanup, lambda _value: self.refresh(), feedback=self.feedback
        )

    def refresh(self) -> None:
        selected = {(copy.generation, copy.cache_id) for copy in self._selected()}
        current_item = self.list.currentItem()
        current = current_item.data(Qt.ItemDataRole.UserRole) if current_item else None
        current_key = (current.generation, current.cache_id) if current else None
        scroll = self.list.verticalScrollBar().value()
        self.list.blockSignals(True)
        self.list.clear()
        page = self.lifecycle.cache.page_info(self.offset, asset_id=self.asset_id)
        while not page.items and not page.unavailable_count and self.offset > 0:
            self.offset = max(0, self.offset - 100)
            page = self.lifecycle.cache.page_info(self.offset, asset_id=self.asset_id)
        self.has_next = page.has_next
        self.navigation.setVisible(self.offset > 0 or page.has_next)
        self.previous_button.setEnabled(self.offset > 0)
        self.next_button.setEnabled(page.has_next)
        self.page_label.setText(f"{self.offset // 100 + 1}페이지")
        for copy in page.items:
            state = {
                "normal": "수정 없음",
                "modified": "저장된 변경 있음",
                "preparing": "준비 중단",
                "unavailable": "확인 불가",
            }[copy.state]
            item = QListWidgetItem()
            status = "이전 복원" if copy.generation != self.lifecycle.cache.generation else state
            set_file_row(
                item,
                copy.name,
                "임시 사본",
                status,
                tooltip=f"{copy.name}\n{state}\n{'문서 닫음 확인' if copy.closed else '저장하고 닫은 후 수정본 저장을 누르세요.'}",
            )
            item.setData(Qt.ItemDataRole.UserRole, copy)
            self.list.addItem(item)
            key = copy.generation, copy.cache_id
            if key == current_key:
                self.list.setCurrentItem(item)
            if key in selected:
                item.setSelected(True)
        if self.list.currentItem() is None and self.list.count():
            preferred = next(
                (
                    row
                    for row in range(self.list.count())
                    if self.list.item(row).data(Qt.ItemDataRole.UserRole).state == "modified"
                ),
                0,
            )
            self.list.setCurrentRow(preferred)
        self.list.blockSignals(False)
        self.list.verticalScrollBar().setValue(scroll)
        count, size, protected = self.lifecycle.cache.usage()
        self.usage_label.setText(
            f"사본 {count}개 · 변경/준비 중 {protected}개"
            + (f" · 상태 확인 필요 {page.unavailable_count}개" if page.unavailable_count else "")
        )
        self.usage_label.setToolTip(
            f"확인된 용량 {size / 1024**2:.1f}MiB · 미수정 생성 상한 100개/512MiB. 단일 큰 파일은 사본이 없을 때만 허용합니다."
        )
        self._update_actions()

    def current(self) -> ViewCopy | None:
        copies = self._selected()
        return copies[0] if len(copies) == 1 else None

    def previous(self) -> None:
        self.offset = max(0, self.offset - 100)
        self.refresh()

    def next(self) -> None:
        if self.has_next:
            self.offset += 100
            self.refresh()

    def check(self) -> None:
        copy = self.current()
        if copy and not self._busy():
            file_actions(self, self.lifecycle).run(
                "사본 확인",
                lambda canceled: self.lifecycle.cache.observe(copy, canceled),
                lambda _value: self.refresh(),
                feedback=self.feedback,
            )

    def confirm(self) -> None:
        self.confirm_selected()

    def preserve(self) -> None:
        copy = self.current()
        if not copy or self._busy():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("수정한 내용을 따로 저장")
        root = QVBoxLayout(dialog)
        hint = QLabel("원본은 그대로 두고 수정한 내용을 새 작업본으로 저장합니다.")
        hint.setWordWrap(True)
        root.addWidget(hint)
        name = QLineEdit(self.lifecycle.catalog.work_suggestion(copy.task_id, copy.name))
        root.addWidget(name)
        closed = QCheckBox("편집기에서 저장하고 문서를 닫았습니다.")
        closed.setChecked(copy.closed)
        root.addWidget(closed)
        button = QPushButton("저장")
        button.setEnabled(closed.isChecked())
        closed.toggled.connect(button.setEnabled)
        root.addWidget(button)

        def submit() -> None:
            value = name.text()

            def preserve(canceled: Callable[[], bool]) -> object:
                confirmed = self.lifecycle.cache.confirm_closed(copy, copy.use_token)
                result = self.lifecycle.cache.preserve(
                    confirmed, value, self.lifecycle.documents, canceled
                )
                self.lifecycle.changed(copy.task_id)
                return result

            file_actions(self, self.lifecycle).run(
                "작업본 보존",
                preserve,
                after_change(self.refresh, dialog),
                feedback=self.feedback,
            )

        button.clicked.connect(submit)
        file_actions(self, self.lifecycle).keep(dialog)

    def discard(self) -> None:
        copy = self.current()
        if not copy or self._busy():
            return

        def observe(canceled: Callable[[], bool]) -> ViewCopy:
            current = self.lifecycle.cache.get(copy.generation, copy.cache_id)
            if current.use_token != copy.use_token:
                raise ValueError("사본을 다시 열었습니다. 새로고침 후 다시 확인하세요.")
            return self.lifecycle.cache.observe(current, canceled)

        file_actions(self, self.lifecycle).run(
            "버릴 사본 확인",
            observe,
            self._confirm_discard,
            feedback=self.feedback,
            close_on_success=True,
        )

    def _confirm_discard(self, copy: ViewCopy) -> None:
        dialog = QDialog(self)
        dialog.setObjectName("viewDiscardConfirmation")
        dialog.setWindowTitle("보기 사본 정리 확인")
        dialog.resize(440, 240)
        root = QVBoxLayout(dialog)
        filename = QLineEdit(copy.name)
        filename.setReadOnly(True)
        filename.setToolTip(copy.name)
        root.addWidget(filename)
        label = QLabel(
            "선택한 사본의 변경 내용을 버립니다. 되돌릴 수 없습니다.\n"
            "원본/보관본은 그대로 남습니다. 먼저 편집기에서 이 문서를 닫아 주세요."
        )
        label.setWordWrap(True)
        root.addWidget(label)
        closed = QCheckBox("편집기에서 문서를 닫았습니다.")
        root.addWidget(closed)
        button = QPushButton("선택한 사본만 삭제")
        button.setObjectName("discardViewButton")
        button.setEnabled(False)
        closed.toggled.connect(button.setEnabled)
        actions = QHBoxLayout()
        actions.addWidget(button)
        cancel = QPushButton("취소")
        cancel.clicked.connect(dialog.reject)
        actions.addWidget(cancel)
        root.addLayout(actions)

        def discarded(_value: object) -> None:
            self.lifecycle.changed(copy.task_id)
            self.refresh()
            dialog.accept()

        def submit() -> None:
            consent = closed.isChecked()
            file_actions(self, self.lifecycle).run(
                "사본 정리",
                lambda canceled: self.lifecycle.cache.discard_after_close(
                    copy, closed_consent=consent, canceled=canceled
                ),
                discarded,
                feedback=self.feedback,
            )

        button.clicked.connect(submit)
        file_actions(self, self.lifecycle).keep(dialog)

    def shutdown(self) -> None:
        stop_file_actions(self)
        if self.exporter:
            self.exporter.shutdown()

    def done(self, result: int) -> None:
        self.shutdown()
        super().done(result)
