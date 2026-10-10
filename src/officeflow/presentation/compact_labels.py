"""Width-bounded, plain-text summaries with complete selectable detail elsewhere."""

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget


class CompactLabel(QLabel):
    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        elide_mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
    ) -> None:
        super().__init__(parent)
        self._elide_mode = elide_mode
        self.full_text = text
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self.setText(text)

    def setText(self, text: str) -> None:
        self.full_text = text
        self.setToolTip(text)
        self.setAccessibleName(text)
        self._render()

    def _render(self) -> None:
        metrics = self.fontMetrics()
        lines = self.full_text.splitlines()
        super().setText(
            "\n".join(
                metrics.elidedText(line, self._elide_mode, max(1, self.contentsRect().width()))
                for line in lines
            )
        )
        self.setMaximumHeight(max(1, len(lines)) * metrics.lineSpacing() + 6)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._render()


class WrappingLabel(QLabel):
    """Keep mandatory summary notices visible as the available width changes."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)

    def setText(self, text: str) -> None:
        super().setText(text)
        self._fit_height()

    def _fit_height(self) -> None:
        if self.width() > 50:
            height = max(self.fontMetrics().lineSpacing() + 4, self.heightForWidth(self.width()))
            if self.minimumHeight() != height:
                self.setMinimumHeight(height)
                self.updateGeometry()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit_height()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.StyleChange}:
            self._fit_height()
