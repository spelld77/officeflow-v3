from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import cast
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QObject, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.attachments import (
    AttachmentCanceledError,
    AttachmentIntegrity,
    AttachmentOperationError,
    AttachmentService,
)
from officeflow.application.records import RecordService
from officeflow.application.tasks import TaskService
from officeflow.domain.attachment import Attachment
from officeflow.domain.records import ChecklistItem, RecordValidationError, WorkLog
from officeflow.domain.task import Task


def _qdate(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


class AttachmentImportWorker(QObject):
    succeeded = Signal(object)
    failed = Signal(object)
    finished = Signal()

    def __init__(self, service: AttachmentService, task_id: int, source: Path) -> None:
        super().__init__()
        self._service = service
        self._task_id = task_id
        self._source = source
        self._canceled = Event()

    def cancel(self) -> None:
        self._canceled.set()

    @Slot()
    def run(self) -> None:
        try:
            attachment = self._service.attach(
                self._task_id,
                self._source,
                cancel_requested=self._canceled.is_set,
            )
        except Exception as error:
            self.failed.emit(error)
        else:
            self.succeeded.emit(attachment)
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
        help_label = QLabel("결과를 바로 남기고 완료할 수 있습니다. 결과 입력은 선택 사항입니다.")
        help_label.setObjectName("mutedText")
        help_label.setWordWrap(True)
        root.addWidget(help_label)
        self.result_edit = QTextEdit()
        self.result_edit.setObjectName("completionResultEdit")
        self.result_edit.setPlaceholderText("완료 결과, 결정 사항 또는 다음 할 일 (선택)")
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

    def __init__(
        self,
        task: Task,
        *,
        task_service: TaskService,
        record_service: RecordService,
        attachment_service: AttachmentService | None = None,
        occurrence_start: datetime | None = None,
        initial_tab: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if task.id is None:
            raise ValueError("저장된 업무만 기록을 작성할 수 있습니다.")
        self._task = task
        self._task_id = task.id
        self._read_only = task.deleted_at is not None
        self._task_service = task_service
        self._record_service = record_service
        self._attachment_service = attachment_service
        self._occurrence_start = occurrence_start
        self._checklist_by_id: dict[int, ChecklistItem] = {}
        self._work_logs_by_id: dict[int, WorkLog] = {}
        self._selected_log_id: int | None = None
        self._loading_checklist = False
        self._attachments_by_id: dict[int, Attachment] = {}
        self._selected_attachment_id: int | None = None
        self._attachment_thread: QThread | None = None
        self._attachment_worker: AttachmentImportWorker | None = None
        self._attachment_progress: QProgressDialog | None = None

        self.setWindowTitle(f"업무 기록 · {task.title}")
        self.setModal(True)
        self.resize(620, 650)
        self.setMinimumSize(500, 540)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        heading = QLabel(task.title)
        heading.setObjectName("pageTitle")
        heading.setWordWrap(True)
        root.addWidget(heading)
        context = "이 반복 일정 건의 기록" if occurrence_start is not None else "업무 전체 기록"
        if self._read_only:
            context += " · 휴지통에서는 조회만 가능하며 수정하려면 먼저 복원하세요."
        caption = QLabel(context)
        caption.setObjectName("mutedText")
        root.addWidget(caption)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("taskRecordsTabs")
        self.tabs.addTab(self._build_checklist_tab(), "체크리스트")
        self.tabs.addTab(self._build_result_tab(), "결과 메모")
        self.tabs.addTab(self._build_work_log_tab(), "업무일지")
        if attachment_service is not None:
            self.tabs.addTab(self._build_attachment_tab(), "첨부파일")
        tab_indexes = {"checklist": 0, "result": 1, "work_log": 2, "attachments": 3}
        requested_index = tab_indexes.get(initial_tab or "")
        if requested_index is not None and requested_index < self.tabs.count():
            self.tabs.setCurrentIndex(requested_index)
        root.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._refresh_checklist()
        self.result_edit.setPlainText(
            task_service.result_note(self._task_id, occurrence_start)
        )
        self._refresh_work_logs()
        if attachment_service is not None:
            self._refresh_attachments()
        if self._read_only:
            self._apply_read_only_mode()

    def _apply_read_only_mode(self) -> None:
        self.checklist_edit.setReadOnly(True)
        self.checklist_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for index in range(self.checklist_list.count()):
            item = self.checklist_list.item(index)
            item.setFlags(
                item.flags()
                & ~Qt.ItemFlag.ItemIsEditable
                & ~Qt.ItemFlag.ItemIsUserCheckable
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
        layout.addWidget(self.checklist_list, 1)

        actions = QHBoxLayout()
        up_button = QPushButton("위로")
        up_button.clicked.connect(lambda: self._move_checklist(-1))
        down_button = QPushButton("아래로")
        down_button.clicked.connect(lambda: self._move_checklist(1))
        delete_button = QPushButton("선택 삭제")
        delete_button.setObjectName("deleteChecklistButton")
        delete_button.clicked.connect(self._delete_checklist)
        actions.addWidget(up_button)
        actions.addWidget(down_button)
        actions.addStretch()
        actions.addWidget(delete_button)
        layout.addLayout(actions)
        return tab

    def _build_result_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 14, 10, 10)
        help_label = QLabel("업무를 마친 결과, 결정 사항이나 다음 행동을 남겨 두세요.")
        help_label.setObjectName("mutedText")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        self.result_edit = QTextEdit()
        self.result_edit.setObjectName("resultNoteEdit")
        self.result_edit.setPlaceholderText("결과 메모")
        self.result_edit.setTabChangesFocus(True)
        layout.addWidget(self.result_edit, 1)
        save_button = QPushButton("결과 메모 저장")
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
        form.addRow("날짜", self.log_date_edit)
        self.log_content_edit = QTextEdit()
        self.log_content_edit.setObjectName("workLogContent")
        self.log_content_edit.setPlaceholderText("오늘 진행한 내용을 입력하세요")
        self.log_content_edit.setMaximumHeight(90)
        self.log_content_edit.setTabChangesFocus(True)
        form.addRow("진행 내용 *", self.log_content_edit)
        self.log_result_edit = QTextEdit()
        self.log_result_edit.setObjectName("workLogResult")
        self.log_result_edit.setPlaceholderText("성과, 이슈 또는 다음 할 일")
        self.log_result_edit.setMaximumHeight(75)
        self.log_result_edit.setTabChangesFocus(True)
        form.addRow("결과", self.log_result_edit)
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
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 14, 10, 10)
        help_label = QLabel(
            "원본을 앱 관리 폴더에 복사합니다. 연결 해제는 파일을 남기고, 파일 삭제는 실제 파일도 제거합니다."
        )
        help_label.setObjectName("mutedText")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)

        self.add_attachment_button = QPushButton("파일 첨부")
        self.add_attachment_button.setObjectName("addAttachmentButton")
        self.add_attachment_button.setProperty("primaryAction", True)
        self.add_attachment_button.clicked.connect(self._choose_attachment)
        layout.addWidget(self.add_attachment_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self.attachment_list = QListWidget()
        self.attachment_list.setObjectName("attachmentList")
        self.attachment_list.currentItemChanged.connect(self._attachment_selected)
        self.attachment_list.itemDoubleClicked.connect(lambda _item: self._open_attachment())
        layout.addWidget(self.attachment_list, 1)

        self.attachment_detail = QLabel("첨부파일을 선택하세요.")
        self.attachment_detail.setObjectName("attachmentDetail")
        self.attachment_detail.setWordWrap(True)
        self.attachment_detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.attachment_detail)

        actions = QHBoxLayout()
        self.open_attachment_button = QPushButton("열기")
        self.open_attachment_button.setObjectName("openAttachmentButton")
        self.open_attachment_button.clicked.connect(self._open_attachment)
        self.verify_attachment_button = QPushButton("무결성 확인")
        self.verify_attachment_button.setObjectName("verifyAttachmentButton")
        self.verify_attachment_button.clicked.connect(self._verify_attachment)
        self.unlink_attachment_button = QPushButton("연결 해제")
        self.unlink_attachment_button.setObjectName("unlinkAttachmentButton")
        self.unlink_attachment_button.clicked.connect(self._unlink_attachment)
        self.delete_attachment_button = QPushButton("파일 삭제")
        self.delete_attachment_button.setObjectName("deleteAttachmentButton")
        self.delete_attachment_button.clicked.connect(self._delete_attachment)
        for button in (
            self.open_attachment_button,
            self.verify_attachment_button,
            self.unlink_attachment_button,
            self.delete_attachment_button,
        ):
            button.setEnabled(False)
            actions.addWidget(button)
        layout.addLayout(actions)
        return tab

    def _refresh_checklist(self) -> None:
        items = self._record_service.checklist_for_task(self._task_id)
        self._loading_checklist = True
        self.checklist_list.clear()
        self._checklist_by_id = {item.id: item for item in items if item.id is not None}
        for checklist in items:
            item = QListWidgetItem(checklist.content)
            item.setFlags(
                item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEditable
            )
            item.setCheckState(
                Qt.CheckState.Checked if checklist.is_done else Qt.CheckState.Unchecked
            )
            item.setData(Qt.ItemDataRole.UserRole, checklist.id)
            self.checklist_list.addItem(item)
        self._loading_checklist = False

    def _add_checklist(self) -> None:
        try:
            self._record_service.add_checklist_item(
                self._task_id, self.checklist_edit.text()
            )
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
        item = self.checklist_list.currentItem()
        if item is None:
            return
        item_id = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(item_id, int):
            return
        self._record_service.delete_checklist_item(item_id)
        self._refresh_checklist()
        self.changed.emit()

    def _move_checklist(self, direction: int) -> None:
        row = self.checklist_list.currentRow()
        target = row + direction
        if row < 0 or target < 0 or target >= self.checklist_list.count():
            return
        item_ids = [
            self.checklist_list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.checklist_list.count())
        ]
        item_ids[row], item_ids[target] = item_ids[target], item_ids[row]
        self._record_service.reorder_checklist(
            self._task_id, tuple(item_id for item_id in item_ids if isinstance(item_id, int))
        )
        self._refresh_checklist()
        self.checklist_list.setCurrentRow(target)
        self.changed.emit()

    def _save_result(self) -> None:
        try:
            self._task_service.update_result_note(
                self._task_id,
                self.result_edit.toPlainText(),
                occurrence_start=self._occurrence_start,
            )
        except (LookupError, ValueError) as error:
            QMessageBox.warning(self, "결과 메모를 저장하지 못했습니다.", str(error))
            return
        self.changed.emit()
        QMessageBox.information(self, "저장 완료", "결과 메모를 저장했습니다.")

    def _refresh_work_logs(self) -> None:
        logs = self._record_service.work_logs(task_id=self._task_id)
        self._work_logs_by_id = {log.id: log for log in logs if log.id is not None}
        self.work_log_list.clear()
        for log in logs:
            preview = " ".join(log.content.splitlines())
            item = QListWidgetItem(f"{log.log_date:%Y-%m-%d}  {preview}")
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

    def _save_work_log(self) -> None:
        try:
            if self._selected_log_id is None:
                self._record_service.add_work_log(
                    task_id=self._task_id,
                    log_date=cast(date, self.log_date_edit.date().toPython()),
                    content=self.log_content_edit.toPlainText(),
                    result=self.log_result_edit.toPlainText(),
                )
            else:
                self._record_service.update_work_log(
                    self._selected_log_id,
                    log_date=cast(date, self.log_date_edit.date().toPython()),
                    content=self.log_content_edit.toPlainText(),
                    result=self.log_result_edit.toPlainText(),
                )
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

    def _refresh_attachments(self) -> None:
        if self._attachment_service is None:
            return
        attachments = self._attachment_service.attachments_for_task(self._task_id)
        self._attachments_by_id = {
            attachment.id: attachment
            for attachment in attachments
            if attachment.id is not None
        }
        self.attachment_list.clear()
        for attachment in attachments:
            status = "누락됨" if attachment.missing_at is not None else "사용 가능"
            item = QListWidgetItem(
                f"{attachment.original_name}  ·  {self._format_size(attachment.size_bytes)}  ·  {status}"
            )
            item.setData(Qt.ItemDataRole.UserRole, attachment.id)
            if attachment.missing_at is not None:
                item.setForeground(Qt.GlobalColor.darkRed)
            self.attachment_list.addItem(item)
        self._selected_attachment_id = None
        self.attachment_detail.setText(
            "첨부파일이 없습니다."
            if not attachments
            else "첨부파일을 선택하면 저장 정보와 상태를 확인할 수 있습니다."
        )
        self._set_attachment_actions_enabled(False)

    def _choose_attachment(self) -> None:
        source, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "첨부할 파일 선택",
            "",
            "모든 파일 (*.*)",
        )
        if not source:
            return
        source_path = Path(source)
        try:
            size_bytes = source_path.stat().st_size
        except OSError as error:
            QMessageBox.warning(self, "파일을 첨부하지 못했습니다.", str(error))
            return
        if size_bytes >= 100 * 1024 * 1024:
            answer = QMessageBox.question(
                self,
                "큰 첨부파일",
                f"'{source_path.name}'은 {self._format_size(size_bytes)}입니다.\n"
                "백업 크기와 다른 PC로 옮기는 시간이 크게 늘어날 수 있습니다. "
                "그래도 첨부할까요?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._start_attachment_import(source_path)

    def _start_attachment_import(self, source: Path) -> None:
        if self._attachment_service is None or self._attachment_thread is not None:
            return
        thread = QThread(self)
        worker = AttachmentImportWorker(self._attachment_service, self._task_id, source)
        worker.moveToThread(thread)
        progress = QProgressDialog(
            f"'{source.name}' 파일을 안전하게 복사하고 있습니다.",
            "취소",
            0,
            0,
            self,
        )
        progress.setWindowTitle("첨부파일 복사")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.canceled.connect(lambda: worker.cancel())
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._attachment_imported)
        worker.failed.connect(self._attachment_import_failed)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._attachment_import_finished)
        self._attachment_thread = thread
        self._attachment_worker = worker
        self._attachment_progress = progress
        self.add_attachment_button.setEnabled(False)
        progress.show()
        thread.start()

    @Slot(object)
    def _attachment_imported(self, _attachment: object) -> None:
        self._refresh_attachments()
        self.changed.emit()

    @Slot(object)
    def _attachment_import_failed(self, error: object) -> None:
        if isinstance(error, AttachmentCanceledError):
            return
        message = str(error) if isinstance(error, Exception) else "알 수 없는 오류"
        QMessageBox.warning(self, "파일을 첨부하지 못했습니다.", message)

    @Slot()
    def _attachment_import_finished(self) -> None:
        if self._attachment_progress is not None:
            self._attachment_progress.close()
        if hasattr(self, "add_attachment_button"):
            self.add_attachment_button.setEnabled(not self._read_only)
        thread = self._attachment_thread
        self._attachment_thread = None
        self._attachment_worker = None
        self._attachment_progress = None
        if thread is not None:
            thread.deleteLater()

    def _attachment_selected(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        if current is None:
            self._selected_attachment_id = None
            self._set_attachment_actions_enabled(False)
            return
        attachment_id = current.data(Qt.ItemDataRole.UserRole)
        attachment = self._attachments_by_id.get(attachment_id)
        if attachment is None:
            return
        self._selected_attachment_id = attachment.id
        checksum = attachment.checksum or "아직 계산되지 않음"
        status = "파일 누락" if attachment.missing_at is not None else "사용 가능"
        self.attachment_detail.setText(
            f"상태: {status}\n크기: {self._format_size(attachment.size_bytes)}\n"
            f"SHA-256: {checksum}"
        )
        self._set_attachment_actions_enabled(True)
        self.open_attachment_button.setEnabled(attachment.missing_at is None)

    def _open_attachment(self) -> None:
        if self._selected_attachment_id is None or self._attachment_service is None:
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
        if self._selected_attachment_id is None or self._attachment_service is None:
            return
        try:
            result = self._attachment_service.verify(self._selected_attachment_id)
        except (AttachmentOperationError, LookupError, OSError) as error:
            QMessageBox.warning(self, "무결성을 확인하지 못했습니다.", str(error))
            return
        if result.integrity is AttachmentIntegrity.VERIFIED:
            title, message = "확인 완료", "원본 첨부 시점과 파일 내용이 일치합니다."
            QMessageBox.information(self, title, message)
        elif result.integrity is AttachmentIntegrity.MODIFIED:
            QMessageBox.warning(
                self,
                "파일 변경 감지",
                "첨부 후 파일 내용이 변경되어 SHA-256 체크섬이 일치하지 않습니다.",
            )
        else:
            QMessageBox.warning(self, "파일 누락", "관리 폴더에서 파일을 찾지 못했습니다.")
        self._refresh_attachments()

    def _unlink_attachment(self) -> None:
        if self._selected_attachment_id is None or self._attachment_service is None:
            return
        attachment = self._attachments_by_id.get(self._selected_attachment_id)
        if attachment is None:
            return
        answer = QMessageBox.question(
            self,
            "첨부 연결 해제",
            f"'{attachment.original_name}'을 정리 대기함으로 옮길까요?\n"
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
        if self._selected_attachment_id is None or self._attachment_service is None:
            return
        attachment = self._attachments_by_id.get(self._selected_attachment_id)
        if attachment is None:
            return
        answer = QMessageBox.warning(
            self,
            "첨부파일 영구 삭제",
            f"'{attachment.original_name}'의 연결 정보와 실제 파일을 삭제합니다.\n이 동작은 되돌릴 수 없습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._attachment_service.delete_file(attachment.id_required)
        except (AttachmentOperationError, LookupError, OSError) as error:
            QMessageBox.warning(self, "첨부파일을 삭제하지 못했습니다.", str(error))
            self._refresh_attachments()
            return
        self._refresh_attachments()
        self.changed.emit()

    def _set_attachment_actions_enabled(self, enabled: bool) -> None:
        self.open_attachment_button.setEnabled(enabled)
        self.verify_attachment_button.setEnabled(enabled)
        self.unlink_attachment_button.setEnabled(enabled and not self._read_only)
        self.delete_attachment_button.setEnabled(enabled and not self._read_only)

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
    SEARCH_BATCH_SIZE = 25

    def __init__(
        self,
        *,
        task_service: TaskService,
        record_service: RecordService,
        attachment_service: AttachmentService | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._task_service = task_service
        self._record_service = record_service
        self._attachment_service = attachment_service
        self._logs_by_id: dict[int, WorkLog] = {}
        self._completed_by_key: dict[tuple[int, str], Task] = {}
        self._selected_task_context: tuple[int, datetime | None] | None = None
        self._loaded_completed: list[Task] = []
        self._loaded_logs: list[WorkLog] = []
        self._completed_offset = 0
        self._log_offset = 0
        self._search_total = 0
        self.setWindowTitle("날짜별 업무일지")
        self.resize(680, 560)
        self.setMinimumSize(500, 440)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        title = QLabel("업무일지")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("workLogSearch")
        self.search_edit.setPlaceholderText("과거 업무 검색: 제목, 설명, 결과, 업무일지 내용")
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
        self.range_checkbox = QCheckBox("기간 지정")
        self.range_checkbox.setObjectName("workLogRangeEnabled")
        self.range_checkbox.toggled.connect(self._refresh)
        range_controls.addWidget(self.range_checkbox)
        self.range_from_edit = QDateEdit(QDate.currentDate().addYears(-1))
        self.range_from_edit.setObjectName("workLogRangeFrom")
        self.range_from_edit.setCalendarPopup(True)
        self.range_from_edit.setDisplayFormat("yyyy-MM-dd")
        self.range_from_edit.dateChanged.connect(self._refresh)
        range_controls.addWidget(self.range_from_edit, 1)
        range_controls.addWidget(QLabel("~"))
        self.range_to_edit = QDateEdit(QDate.currentDate())
        self.range_to_edit.setObjectName("workLogRangeTo")
        self.range_to_edit.setCalendarPopup(True)
        self.range_to_edit.setDisplayFormat("yyyy-MM-dd")
        self.range_to_edit.dateChanged.connect(self._refresh)
        range_controls.addWidget(self.range_to_edit, 1)
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
        controls.addWidget(self.date_edit, 1)
        controls.addWidget(self.next_button)
        root.addWidget(self.day_controls_widget)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("workLogBrowserList")
        self.list_widget.currentItemChanged.connect(self._show_selected)
        self.list_widget.itemDoubleClicked.connect(lambda _item: self._open_selected_records())
        root.addWidget(self.list_widget, 1)
        self.load_more_button = QPushButton("검색 결과 더 보기")
        self.load_more_button.setObjectName("workLogLoadMoreButton")
        self.load_more_button.clicked.connect(self._load_more_search)
        self.load_more_button.hide()
        root.addWidget(self.load_more_button, alignment=Qt.AlignmentFlag.AlignCenter)
        self.detail = QLabel("날짜를 선택하면 기록을 확인할 수 있습니다.")
        self.detail.setObjectName("workLogBrowserDetail")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.detail)
        self.open_records_button = QPushButton("결과 · 기록 열기")
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
            self.detail.setText("검색 시작일은 종료일보다 늦을 수 없습니다.")
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
        self._search_total = completed_page.total + log_page.total
        self._render_entries(
            self._loaded_completed,
            self._loaded_logs,
            searching=True,
        )
        has_more = completed_page.has_more or log_page.has_more
        self.load_more_button.setVisible(has_more)
        self.load_more_button.setText(
            f"더 보기 ({len(self._loaded_completed) + len(self._loaded_logs):,} / "
            f"{self._search_total:,})"
        )

    def _render_entries(
        self,
        completed: list[Task],
        logs: list[WorkLog],
        *,
        searching: bool,
    ) -> None:
        self._logs_by_id = {log.id: log for log in logs if log.id is not None}
        self._completed_by_key.clear()
        self._selected_task_context = None
        self.open_records_button.setEnabled(False)
        self.list_widget.clear()
        for task in completed:
            if task.id is None:
                continue
            occurrence_start = task.starts_at if task.recurrence_rule else None
            occurrence_key = occurrence_start.isoformat() if occurrence_start is not None else ""
            key = (task.id, occurrence_key)
            self._completed_by_key[key] = task
            result_note = self._task_service.result_note(task.id, occurrence_start)
            result_state = "결과 있음" if result_note else "결과 미입력"
            completed_date = (
                task.completed_at.astimezone(ZoneInfo(task.timezone)).date().isoformat()
                if task.completed_at is not None
                else "날짜 미상"
            )
            prefix = f"[완료 {completed_date}]" if searching else "[완료]"
            item = QListWidgetItem(f"{prefix} {task.title}  ·  {result_state}")
            item.setData(Qt.ItemDataRole.UserRole, ("task", *key))
            self.list_widget.addItem(item)
        task_ids = tuple({log.task_id for log in logs if log.task_id is not None})
        tasks_by_id = self._task_service.get_many_including_deleted(task_ids)
        for log in logs:
            task_title = "연결되지 않은 기록"
            if log.task_id is not None:
                log_task = tasks_by_id.get(log.task_id)
                task_title = log_task.title if log_task is not None else "삭제된 업무"
            preview = " ".join(log.content.splitlines())
            prefix = f"[일지 {log.log_date.isoformat()}]" if searching else "[일지]"
            item = QListWidgetItem(f"{prefix} {task_title}  ·  {preview}")
            item.setData(Qt.ItemDataRole.UserRole, ("log", log.id))
            self.list_widget.addItem(item)
        self.detail.setText(
            (
                "검색 결과가 없습니다. 제목, 설명, 결과 또는 업무일지 내용으로 검색해 보세요."
                if searching
                else "이 날짜에 완료한 업무나 작성한 업무일지가 없습니다."
            )
            if not completed and not logs
            else "완료 업무 또는 업무일지를 선택하면 내용을 확인할 수 있습니다."
        )

    def _show_selected(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        if current is None:
            return
        data = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, tuple) or not data:
            return
        if data[0] == "task" and len(data) == 3:
            task_id = int(data[1])
            occurrence_start = datetime.fromisoformat(data[2]) if data[2] else None
            task = self._completed_by_key.get((task_id, str(data[2])))
            if task is None:
                return
            result_note = self._task_service.result_note(task_id, occurrence_start)
            result = result_note or "아직 입력하지 않았습니다."
            self.detail.setText(f"완료 업무\n{task.title}\n\n결과\n{result}")
            self._selected_task_context = (task_id, occurrence_start)
            self.open_records_button.setEnabled(True)
            return
        if data[0] != "log" or len(data) != 2:
            return
        work_log = self._logs_by_id.get(data[1])
        if work_log is None:
            return
        result = f"\n\n결과\n{work_log.result}" if work_log.result else ""
        self.detail.setText(f"진행 내용\n{work_log.content}{result}")
        self._selected_task_context = (
            (work_log.task_id, None) if work_log.task_id is not None else None
        )
        self.open_records_button.setEnabled(self._selected_task_context is not None)

    def _open_selected_records(self) -> None:
        if self._selected_task_context is None:
            return
        task_id, occurrence_start = self._selected_task_context
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
            initial_tab="result",
            parent=self,
        )
        dialog.changed.connect(self._refresh)
        dialog.exec()
        self._refresh()
