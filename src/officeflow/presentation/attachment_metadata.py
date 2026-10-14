"""Small light-themed editors for attachment metadata, never file content."""

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from officeflow.domain.attachment import Attachment, renamed_display_name
from officeflow.presentation.compact_labels import CompactLabel
from officeflow.presentation.theme import LIGHT_STYLESHEET


class AttachmentMetadataDialog(QDialog):
    def __init__(
        self,
        attachment: Attachment,
        *,
        edit_memo: bool = False,
        parent: QWidget | None = None,
        submit: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setStyleSheet(LIGHT_STYLESHEET)
        self.setWindowTitle("첨부 메모" if edit_memo else "첨부 이름 변경")
        self.resize(420, 300 if edit_memo else 220)
        self._edit_memo = edit_memo
        self._submit = submit
        self._extension = Path(attachment.name).suffix
        self._original_name = attachment.name
        self._initial_stem = (
            attachment.name[: -len(self._extension)] if self._extension else attachment.name
        )
        self.value = attachment.memo if edit_memo else attachment.name
        layout = QVBoxLayout(self)
        label = QLabel(attachment.name[:100] + ("…" if len(attachment.name) > 100 else ""))
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        layout.addWidget(label)
        self.name_edit = QLineEdit(self)
        self.memo_edit = QTextEdit(self)
        if edit_memo:
            self.memo_edit.setAcceptRichText(False)
            self.memo_edit.setPlaceholderText("이 파일에 대한 메모 (선택, 최대 2,000자)")
            self.memo_edit.setPlainText(attachment.memo)
            layout.addWidget(self.memo_edit)
            self.name_edit.hide()
        else:
            row = QHBoxLayout()
            self.name_edit.setText(self._initial_stem)
            self.name_edit.setMaxLength(500)
            row.addWidget(self.name_edit, 1)
            row.addWidget(QLabel(self._extension or "(확장자 없음)"))
            layout.addLayout(row)
            self.final_name = CompactLabel()
            self.final_name.setObjectName("attachmentFinalName")
            layout.addWidget(self.final_name)
            self.name_edit.textChanged.connect(self._preview_name)
            self._preview_name()
            self.memo_edit.hide()
        hint = QLabel(
            "파일 내용·업무일지는 바뀌지 않습니다."
            if edit_memo
            else "목록의 이름만 바꿉니다. 실제 파일 경로와 기존 작업본·보관본 이름은 바뀌지 않습니다."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.error_label = QLabel()
        self.error_label.setObjectName("errorLabel")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        if not edit_memo:
            self.name_edit.selectAll()
            self.name_edit.setFocus()

    def _name_value(self) -> str:
        text = self.name_edit.text()
        return (
            self._original_name
            if text == self._initial_stem
            else renamed_display_name(text, self._extension)
        )

    def _preview_name(self) -> None:
        try:
            name = self._name_value()
        except ValueError as error:
            self.final_name.setText(str(error))
        else:
            self.final_name.setText(f"완성 이름: {name}")
            self.final_name.setToolTip(name)

    def _save(self) -> None:
        try:
            if self._edit_memo:
                value = self.memo_edit.toPlainText()
                if len(value) > 2000:
                    raise ValueError("첨부 메모는 2,000자 이하여야 합니다.")
            else:
                value = self._name_value()
            self.value = value
            if self._submit is not None and (self._edit_memo or value != self._original_name):
                self._submit(value)
        except Exception as error:
            # A failed DB submission is not success; keep the user's draft visible.
            self.error_label.setText(str(error))
            return
        self.accept()
