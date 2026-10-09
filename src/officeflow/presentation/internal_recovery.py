"""Uncertain internal materials are visible, not silently adopted or removed."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.attachment_documents import InternalRecoveryItem
from officeflow.application.document_lifecycle import DocumentLifecycle
from officeflow.infrastructure.attachments.deletion_storage import DeletionFileStorage
from officeflow.infrastructure.attachments.internal_storage import InternalFileStorage
from officeflow.presentation.document_actions import file_actions, stop_file_actions
from officeflow.presentation.theme import LIGHT_STYLESHEET


class InternalRecoveryDialog(QDialog):
    def __init__(self, lifecycle: DocumentLifecycle, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.lifecycle = lifecycle
        self.setStyleSheet(LIGHT_STYLESHEET)
        self.setWindowTitle("중단된 내부 작업 확인")
        self.resize(600, 450)
        root = QVBoxLayout(self)
        hint = QLabel(
            "DB 확정 자료만 자동 마무리합니다. 미확정 생성은 선택한 같은 명령을 직접 재시도할 수 있습니다.\n손상/모르는 자료는 삭제하지 않습니다. 전체 백업에는 별도 복구 영역으로 보존됩니다."
        )
        hint.setWordWrap(True)
        root.addWidget(hint)
        self.list = QListWidget()
        self.list.setObjectName("internalRecoveryList")
        self.list.setWordWrap(True)
        root.addWidget(self.list, 1)
        self.feedback = QLabel("")
        self.feedback.setWordWrap(True)
        root.addWidget(self.feedback)
        refresh = QPushButton("목록 확인 (확정 자료는 백그라운드 정리)")
        refresh.clicked.connect(self.refresh)
        root.addWidget(refresh)
        retry = QPushButton("선택한 미확정 생성 재시도 (출처/내용 재검증)")
        retry.clicked.connect(self.retry)
        root.addWidget(retry)
        self.refresh()

    def refresh(self) -> None:
        def inspect(canceled: object) -> object:
            internal = InternalFileStorage(
                self.lifecycle.cache.root, coordinator=self.lifecycle.documents.coordinator
            )
            delete = DeletionFileStorage(
                self.lifecycle.cache.root, coordinator=self.lifecycle.documents.coordinator
            )
            return internal.preflight(), delete.preflight()

        def show(value: object) -> None:
            if not isinstance(value, tuple):
                return
            self.list.clear()
            for item in value[0] + value[1]:
                row = QListWidgetItem(f"{item.job_id or '영역 점검'} · {item.message}")
                row.setData(Qt.ItemDataRole.UserRole, item)
                self.list.addItem(row)
            self.feedback.setText(
                "미확정/손상 자료는 그대로 보존합니다. 확정 작업은 다음 백그라운드 점검에서 마무리됩니다."
            )

        file_actions(self, self.lifecycle).run(
            "중단 자료 확인", inspect, show, feedback=self.feedback
        )

    def retry(self) -> None:
        row = self.list.currentItem()
        item = row.data(Qt.ItemDataRole.UserRole) if row else None
        if isinstance(item, InternalRecoveryItem) and item.command:
            command = item.command
            file_actions(self, self.lifecycle).run(
                "같은 명령 재검증/확정",
                lambda canceled: self.lifecycle.documents.retry_pending(command, canceled),
                lambda _value: self.refresh(),
                feedback=self.feedback,
            )

    def shutdown(self) -> None:
        stop_file_actions(self)

    def done(self, result: int) -> None:
        self.shutdown()
        super().done(result)
