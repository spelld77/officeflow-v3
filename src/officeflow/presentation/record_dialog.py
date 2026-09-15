from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import cast

from PySide6.QtCore import QDate, QObject, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
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
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if task.id is None:
            raise ValueError("저장된 업무만 기록을 작성할 수 있습니다.")
        self._task = task
        self._task_id = task.id
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
        if self._loading_checklist:
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
        if source:
            self._start_attachment_import(Path(source))

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
            self.add_attachment_button.setEnabled(True)
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
            f"'{attachment.original_name}'의 업무 연결만 해제할까요?\n실제 파일은 관리 폴더에 남습니다.",
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
            f"파일은 삭제하지 않았습니다.\n보존 위치: {retained_path}",
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
        for button in (
            self.open_attachment_button,
            self.verify_attachment_button,
            self.unlink_attachment_button,
            self.delete_attachment_button,
        ):
            button.setEnabled(enabled)

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
    def __init__(
        self,
        *,
        task_service: TaskService,
        record_service: RecordService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._task_service = task_service
        self._record_service = record_service
        self._logs_by_id: dict[int, WorkLog] = {}
        self.setWindowTitle("날짜별 업무일지")
        self.resize(680, 560)
        self.setMinimumSize(500, 440)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        title = QLabel("업무일지")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        controls = QHBoxLayout()
        previous = QPushButton("이전 날")
        previous.clicked.connect(lambda: self._move_date(-1))
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setObjectName("workLogBrowserDate")
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.dateChanged.connect(self._refresh)
        next_button = QPushButton("다음 날")
        next_button.clicked.connect(lambda: self._move_date(1))
        controls.addWidget(previous)
        controls.addWidget(self.date_edit, 1)
        controls.addWidget(next_button)
        root.addLayout(controls)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("workLogBrowserList")
        self.list_widget.currentItemChanged.connect(self._show_selected)
        root.addWidget(self.list_widget, 1)
        self.detail = QLabel("날짜를 선택하면 기록을 확인할 수 있습니다.")
        self.detail.setObjectName("workLogBrowserDetail")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.detail)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._refresh()

    def _move_date(self, days: int) -> None:
        current = cast(date, self.date_edit.date().toPython())
        self.date_edit.setDate(_qdate(current + timedelta(days=days)))

    def _refresh(self, _selected: QDate | None = None) -> None:
        logs = self._record_service.work_logs(
            log_date=cast(date, self.date_edit.date().toPython())
        )
        self._logs_by_id = {log.id: log for log in logs if log.id is not None}
        self.list_widget.clear()
        for log in logs:
            task_title = "연결되지 않은 기록"
            if log.task_id is not None:
                try:
                    task_title = self._task_service.get(log.task_id).title
                except LookupError:
                    task_title = "삭제된 업무"
            preview = " ".join(log.content.splitlines())
            item = QListWidgetItem(f"{task_title}  ·  {preview}")
            item.setData(Qt.ItemDataRole.UserRole, log.id)
            self.list_widget.addItem(item)
        self.detail.setText(
            "이 날짜에 작성한 업무일지가 없습니다."
            if not logs
            else "기록을 선택하면 전체 내용을 확인할 수 있습니다."
        )

    def _show_selected(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        if current is None:
            return
        work_log = self._logs_by_id.get(current.data(Qt.ItemDataRole.UserRole))
        if work_log is None:
            return
        result = f"\n\n결과\n{work_log.result}" if work_log.result else ""
        self.detail.setText(f"진행 내용\n{work_log.content}{result}")
