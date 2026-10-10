"""Small disclosure widgets; safety notices stay outside optional help."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from officeflow.presentation.compact_labels import WrappingLabel
from officeflow.presentation.theme import LIGHT_STYLESHEET


class CollapsibleText(QWidget):
    def __init__(self, title: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.toggle_button = QPushButton(f"{title} ▾")
        self.toggle_button.setCheckable(True)
        layout.addWidget(self.toggle_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.label = WrappingLabel()
        self.label.setText(text)
        self.label.setObjectName("mutedText")
        self.label.hide()
        layout.addWidget(self.label)
        self.toggle_button.toggled.connect(self._toggle)

    def _toggle(self, expanded: bool) -> None:
        self.label.setVisible(expanded)
        self.toggle_button.setText(f"{self._title} {'▴' if expanded else '▾'}")


class TextDetailsDialog(QDialog):
    def __init__(self, title: str, text: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setStyleSheet(LIGHT_STYLESHEET)
        self.resize(560, 380)
        self.setMinimumSize(360, 240)
        layout = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlainText(text)
        self.text.setStyleSheet("background:#FFFFFF;color:#172033;")
        self.text.viewport().setStyleSheet("background:#FFFFFF;color:#172033;")
        layout.addWidget(self.text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)


def show_text_details(parent: QWidget, title: str, text: str) -> TextDetailsDialog:
    dialog = TextDetailsDialog(title, text, parent)
    dialog.show()
    return dialog
