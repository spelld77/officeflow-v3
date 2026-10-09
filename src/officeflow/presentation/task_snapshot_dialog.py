"""Manual capture and read-only frozen bundle files; bounded list, explicit actions."""

from __future__ import annotations

from datetime import datetime
from threading import Event
from typing import Any

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.attachment_export import FileReference
from officeflow.application.task_mutations import MutationAttempt
from officeflow.application.task_snapshots import SnapshotCommand, SnapshotSelection
from officeflow.domain.task_history import HistoryEntry
from officeflow.presentation.attachment_export import AttachmentExportController
from officeflow.presentation.dates import format_datetime
from officeflow.presentation.document_actions import file_actions, stop_file_actions
from officeflow.presentation.file_list import checkable_file_list
from officeflow.presentation.task_history_dialog import HistoryCompareDialog, _ReadDialog


class TaskSnapshotDialog(_ReadDialog):
    progressChanged = Signal(int, int)

    def __init__(
        self,
        task_id: int,
        service: Any,
        *,
        occurrence_start: datetime | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.task_id, self.service, self.occurrence_start = task_id, service, occurrence_start
        self._selection: set[SnapshotSelection] = set()
        self._offset = 0
        self._has_next = False
        self._capturing = False
        self._attempt = MutationAttempt()
        self._cancel_capture = Event()
        self.progressChanged.connect(self._progress, Qt.ConnectionType.QueuedConnection)
        self.setWindowTitle("현재 상태 보관")
        self.resize(600, 500)
        self.setMinimumSize(420, 350)
        root = QVBoxLayout(self)
        hint = QLabel(
            "저장된 업무 정보·결과메모·체크리스트를 보관합니다.\n알림 설정·미저장 문서 내용은 복원 대상이 아닙니다."
        )
        hint.setWordWrap(True)
        root.addWidget(hint)
        self.name = QLineEdit()
        self.name.setMaxLength(200)
        self.name.setPlaceholderText("보관 이름: 팀장 검토 전, 최종 제출 자료 등")
        root.addWidget(self.name)
        self.kind = QComboBox()
        self.kind.addItem("현재 첨부", "attachment")
        self.kind.addItem("개별 보관본", "version")
        self.kind.currentIndexChanged.connect(self.reset_page)
        root.addWidget(self.kind)
        self.file_hint = QLabel("첨부파일도 보관하려면 왼쪽 □를 체크하세요.")
        self.file_hint.setWordWrap(True)
        root.addWidget(self.file_hint)
        self.list = QListWidget()
        checkable_file_list(self.list)
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list.itemChanged.connect(self._checked)
        root.addWidget(self.list, 1)
        self.navigation = QWidget()
        navigation = QHBoxLayout(self.navigation)
        self.previous = QPushButton("이전 페이지")
        self.next = QPushButton("다음 페이지")
        self.previous.clicked.connect(lambda: self._change_offset(-100))
        self.next.clicked.connect(lambda: self._change_offset(100))
        navigation.addWidget(self.previous)
        navigation.addWidget(self.next)
        root.addWidget(self.navigation)
        self.count = QLabel("선택 0개 / 최대 100")
        root.addWidget(self.count)
        self.closed = QCheckBox("선택 문서를 저장하고 닫았습니다")
        self.closed.hide()
        root.addWidget(self.closed)
        root.addWidget(self.feedback)
        actions = QHBoxLayout()
        self.capture = QPushButton("업무 정보만 보관")
        self.capture.clicked.connect(self.submit)
        cancel = QPushButton("취소")
        cancel.clicked.connect(self.reject)
        actions.addWidget(self.capture)
        actions.addWidget(cancel)
        root.addLayout(actions)
        self.run(
            lambda: (service.suggestion(task_id), service.selection_page(task_id, "attachment")),
            self._initial,
        )

    def _initial(self, result: Any) -> None:
        self.name.setText(result[0])
        self._render_page(result[1])

    def reset_page(self) -> None:
        self._offset = 0
        self.load_page()

    def _change_offset(self, amount: int) -> None:
        if self._capturing or (amount > 0 and not self._has_next):
            return
        self._offset = max(0, self._offset + amount)
        self.load_page()

    def load_page(self) -> None:
        kind, offset = self.kind.currentData(), self._offset
        self.run(lambda: self.service.selection_page(self.task_id, kind, offset), self._render_page)

    def _render_page(self, page: Any) -> None:
        if not page.items and self._offset > 0:
            self._change_offset(-100)
            return
        blocker = QSignalBlocker(self.list)
        self.list.clear()
        for entry in page.items:
            role = (
                "보관본"
                if entry.kind == "version"
                else {"general": "일반", "work": "작업본", "original": "원본"}[
                    entry.file.role.value
                ]
            )
            text = f"[{role}] " + (entry.label + " · " if entry.label else "") + entry.name
            item = QListWidgetItem(text)
            key = SnapshotSelection(entry.kind, entry.id)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if key in self._selection else Qt.CheckState.Unchecked
            )
            item.setToolTip(text)
            self.list.addItem(item)
        del blocker
        self._has_next = page.has_next
        self.navigation.setVisible(self._offset > 0 or page.has_next)
        self.previous.setEnabled(self._offset > 0)
        self.next.setEnabled(page.has_next)

    def _checked(self, item: QListWidgetItem) -> None:
        key = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            if len(self._selection) == 100 and key not in self._selection:
                with QSignalBlocker(self.list):
                    item.setCheckState(Qt.CheckState.Unchecked)
                self.feedback.setText("최대 100개입니다. 기존 선택을 해제하세요.")
                return
            self._selection.add(key)
        else:
            self._selection.discard(key)
        count = len(self._selection)
        self.count.setText(f"선택 {count}개 / 최대 100 (다른 페이지 포함)")
        self.capture.setText(f"선택 {count}개와 함께 보관" if count else "업무 정보만 보관")
        self.closed.setVisible(count > 0)

    def submit(self) -> None:
        if self._capturing or self._thread is not None:
            return
        try:
            command = SnapshotCommand(
                self._attempt.identify(
                    {
                        "task_id": self.task_id,
                        "label": self.name.text().strip(),
                        "files": sorted((item.kind, item.id) for item in self._selection),
                        "occurrence_start": self.occurrence_start,
                        "documents_closed": self.closed.isChecked(),
                    }
                ),
                self.task_id,
                self.name.text(),
                tuple(self._selection),
                self.occurrence_start,
                self.closed.isChecked(),
            )
        except ValueError as error:
            self.feedback.setText(str(error))
            return
        self._capturing = True
        for widget in (self.name, self.kind, self.list, self.closed, self.capture):
            widget.setEnabled(False)
        self.feedback.setText("업무 정보를 보관하고 있습니다…")
        self.run(
            lambda: self.service.create(
                command, canceled=self._cancel_capture.is_set, progress=self.progressChanged.emit
            ),
            self._captured,
        )

    def _progress(self, done: int, total: int) -> None:
        if not self._closed:
            self.feedback.setText(f"파일 보관 {done} / {total} · 기존 자료는 유지됩니다")

    def _captured(self, result: Any) -> None:
        self._attempt.succeeded()
        self.feedback.setText(
            result.warning
            or (
                "이미 보관된 요청입니다. 같은 보관을 다시 만들지 않았습니다."
                if result.already_committed
                else "중요 시점을 보관했습니다."
            )
        )
        self.capture.setText("보관 완료")

    def _finished(self) -> None:
        super()._finished()
        if self._capturing and not self._closed and self.capture.text() != "보관 완료":
            self._capturing = False
            for widget in (self.name, self.kind, self.list, self.closed, self.capture):
                widget.setEnabled(True)

    def shutdown(self) -> None:
        self._cancel_capture.set()
        super().shutdown()


class SnapshotDetailDialog(_ReadDialog):
    def __init__(
        self,
        entry: HistoryEntry,
        lifecycle: Any,
        export_service: Any,
        *,
        read_only: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.entry, self.lifecycle = entry, lifecycle
        self.read_only = read_only
        self._offset = 0
        self._files: dict[int, Any] = {}
        self._selected: set[int] = set()
        self._info_dialogs: list[HistoryCompareDialog] = []
        self.export = AttachmentExportController(export_service, self) if export_service else None
        self.setWindowTitle("중요 보관 · " + entry.summary.summary)
        self.resize(650, 500)
        self.setMinimumSize(420, 350)
        root = QVBoxLayout(self)
        title = QLabel(entry.summary.summary + f" · 파일 {entry.summary.file_count}개")
        title.setWordWrap(True)
        title.setMaximumHeight(60)
        title.setToolTip(entry.summary.summary)
        root.addWidget(title)
        timezone = entry.after["schedule"].get("timezone") or "Asia/Seoul"
        date = QLabel("보관 당시: " + format_datetime(entry.summary.recorded_at, timezone))
        root.addWidget(date)
        committed_at = (entry.saved_info or {}).get("committed_at")
        if committed_at:
            committed = QLabel(
                "확정 시각: " + format_datetime(datetime.fromisoformat(committed_at), timezone)
            )
            committed.setWordWrap(True)
            root.addWidget(committed)
        info = QPushButton("당시 업무 정보·체크리스트 보기")
        info.clicked.connect(self.show_info)
        root.addWidget(info)
        self.file_hint = QLabel("여러 파일을 저장하려면 왼쪽 □를 체크하세요.")
        self.file_hint.setWordWrap(True)
        root.addWidget(self.file_hint)
        self.list = QListWidget()
        checkable_file_list(self.list)
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.itemChanged.connect(self._checked)
        self.list.itemActivated.connect(lambda _item: self.open_file())
        root.addWidget(self.list, 1)
        self.navigation = QWidget()
        nav = QHBoxLayout(self.navigation)
        self.previous = QPushButton("이전 페이지")
        self.next = QPushButton("다음 페이지")
        self.previous.clicked.connect(lambda: self.load_page(max(0, self._offset - 50)))
        self.next.clicked.connect(lambda: self.load_page(self._offset + 50))
        nav.addWidget(self.previous)
        nav.addWidget(self.next)
        root.addWidget(self.navigation)
        root.addWidget(self.feedback)
        buttons = QGridLayout()
        for index, (label, callback) in enumerate(
            (
                ("파일 보기", self.open_file),
                ("선택 저장…", self.save_files),
                ("새 작업본…", self.create_work),
                ("이 보관 삭제…", self.delete_bundle),
            )
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            button.setEnabled(not read_only or index < 2)
            buttons.addWidget(button, index // 2, index % 2)
        root.addLayout(buttons)
        self.load_page(0)

    def load_page(self, offset: int) -> None:
        self._offset = offset
        summary = self.entry.summary
        self.run(
            lambda: self.lifecycle.catalog.snapshot_files(
                summary.task_id, summary.id, offset=offset
            ),
            self._render,
        )

    def _render(self, page: Any) -> None:
        self._files = {item.id: item for item in page.items}
        blocker = QSignalBlocker(self.list)
        self.list.clear()
        for file in page.items:
            item = QListWidgetItem(file.name + (" · 메모 있음" if file.memo else ""))
            item.setData(Qt.ItemDataRole.UserRole, file.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if file.id in self._selected else Qt.CheckState.Unchecked
            )
            item.setToolTip(file.name + ("\n" + file.memo if file.memo else ""))
            self.list.addItem(item)
        del blocker
        self.navigation.setVisible(self._offset > 0 or page.has_next)
        self.previous.setEnabled(self._offset > 0)
        self.next.setEnabled(page.has_next)
        self.feedback.setText(
            f"파일 {self.entry.summary.file_count}개 · 이 페이지 {len(page.items)}개 · 체크 {len(self._selected)}개"
        )

    def _checked(self, item: QListWidgetItem) -> None:
        identity = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self._selected.add(identity)
        else:
            self._selected.discard(identity)
        self.feedback.setText(
            f"파일 {self.entry.summary.file_count}개 · 이 페이지 {self.list.count()}개 · 체크 {len(self._selected)}개"
        )

    def current_file(self) -> Any:
        item = self.list.currentItem()
        return self._files.get(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def open_file(self) -> None:
        file = self.current_file()
        if file:
            file_actions(self, self.lifecycle).open(
                "snapshot_file", file.id, feedback=self.feedback
            )

    def save_files(self) -> None:
        identities = self._selected or {
            item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems()
        }
        if identities and self.export:
            self.export.choose(
                tuple(FileReference.snapshot_file(identity) for identity in sorted(identities))
            )

    def create_work(self) -> None:
        file = self.current_file()
        if file and not self.read_only:
            file_actions(self, self.lifecycle).create(file, DocumentOperation.WORK, lambda: None)

    def delete_bundle(self) -> None:
        if not self.read_only:
            file_actions(self, self.lifecycle).delete(
                "snapshot", self.entry.summary.id, self.reject
            )

    def show_info(self) -> None:
        timezone = self.entry.after["schedule"].get("timezone") or "Asia/Seoul"
        dialog = HistoryCompareDialog(
            "보관 당시 업무 정보",
            None,
            self.entry.after,
            timezone=timezone,
            right_info=self.entry.saved_info,
            parent=self,
        )
        self._info_dialogs.append(dialog)

        def closed(_code: int) -> None:
            dialog.shutdown()
            if dialog in self._info_dialogs:
                self._info_dialogs.remove(dialog)
            dialog.deleteLater()

        dialog.finished.connect(closed)
        dialog.show()

    def shutdown(self) -> None:
        stop_file_actions(self)
        if self.export:
            self.export.shutdown()
        for dialog in tuple(self._info_dialogs):
            dialog.shutdown()
            dialog.close()
        super().shutdown()


class PendingSnapshotDialog(_ReadDialog):
    def __init__(self, task_id: int, service: Any, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.task_id, self.service = task_id, service
        self._cancel = Event()
        self.setWindowTitle("중단된 중요 보관 확인")
        self.resize(580, 400)
        self.setMinimumSize(420, 300)
        root = QVBoxLayout(self)
        hint = QLabel(
            "완료된 보관은 작업 기록만 마무리합니다. 미완료 보관은 원본·현재 첨부를 그대로 두고, 검증 가능한 준비 파일만 정리합니다.\n복사 도중 중단되었거나 변경된 자료는 자동 삭제하지 않습니다."
        )
        hint.setWordWrap(True)
        root.addWidget(hint)
        self.list = QListWidget()
        self.list.setStyleSheet(
            "QListWidget, QListWidget QWidget {color:#172033; background:#FFFFFF;}"
        )
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        root.addWidget(self.list, 1)
        root.addWidget(self.feedback)
        for label, callback in (
            ("선택 작업 확인·정리…", self.resolve),
            ("최신 조회", self.refresh),
            ("닫기", self.reject),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            root.addWidget(button)
        self.refresh()

    def refresh(self) -> None:
        self.run(lambda: self.service.pending(self.task_id), self._render)

    def _render(self, rows: Any) -> None:
        self.list.clear()
        for identity, label in rows:
            item = QListWidgetItem(label + " · " + identity[:8])
            item.setData(Qt.ItemDataRole.UserRole, identity)
            item.setToolTip(label + "\n" + identity)
            self.list.addItem(item)
        self.feedback.setText(
            "중단 작업 없음"
            if not rows
            else f"확인 필요 {len(rows)}개 · 한 번에 최대 200개 작업 기록을 점검합니다"
        )

    def resolve(self) -> None:
        item = self.list.currentItem()
        if item is None or self._thread is not None:
            return
        identity = item.data(Qt.ItemDataRole.UserRole)
        choice = QMessageBox.question(
            self,
            "중단된 보관 정리",
            "선택한 미완료 보관의 준비 파일을 정리할까요?\n현재 첨부·원본·완료된 보관은 삭제하지 않습니다. 검증할 수 없는 자료는 보존합니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return

        def resolve() -> None:
            raw = self.service.storage.read(identity)
            command = raw["command"]
            request = SnapshotCommand(
                command["job_id"],
                command["task_id"],
                command["label"],
                tuple(SnapshotSelection(row["kind"], row["id"]) for row in command["selections"]),
                datetime.fromisoformat(command["occurrence_start"])
                if command["occurrence_start"]
                else None,
                command["documents_closed"],
            )
            if self.service.committed(request) is not None:
                self.service.recover(identity, self._cancel.is_set)
            else:
                self.service.discard_pending(
                    self.task_id, identity, approved=True, canceled=self._cancel.is_set
                )

        self.run(resolve, lambda _result: self.refresh())

    def shutdown(self) -> None:
        self._cancel.set()
        super().shutdown()
