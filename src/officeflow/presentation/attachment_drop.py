"""One copy-only, queued file-drop route for explicit attachment surfaces."""

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QAbstractButton, QComboBox, QLineEdit, QWidget


class AttachmentDropRouter(QObject):
    def __init__(
        self,
        parent: QWidget,
        surfaces: tuple[QWidget, ...],
        available: Callable[[], bool],
        submit: Callable[[tuple[Path, ...]], None],
        highlight: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._surfaces = surfaces
        self._available = available
        self._submit = submit
        self._highlight = highlight or parent
        self._closed = False
        self.pending = False
        self._sources: tuple[Path, ...] = ()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._deliver)
        for surface in surfaces:
            surface.setAcceptDrops(True)
            surface.installEventFilter(self)

    def clear_highlight(self) -> None:
        self._highlight.setProperty("dragActive", False)
        self._highlight.style().unpolish(self._highlight)
        self._highlight.style().polish(self._highlight)
        self._highlight.update()

    def shutdown(self) -> None:
        self._closed = True
        self._timer.stop()
        self.pending = False
        self._sources = ()
        self.clear_highlight()

    def _deliver(self) -> None:
        sources = self._sources
        self._sources = ()
        self.pending = False
        if not self._closed and self._available():
            self._submit(sources)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(event, (QDragEnterEvent, QDragMoveEvent, QDropEvent, QDragLeaveEvent)):
            return self.handle(watched, event)
        return super().eventFilter(watched, event)

    def handle(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(event, QDragLeaveEvent):
            self.clear_highlight()
            event.accept()
            return True
        if not isinstance(event, (QDragEnterEvent, QDragMoveEvent, QDropEvent)):
            return False
        sources = tuple(
            Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()
        )
        child = (
            watched.childAt(event.position().toPoint()) if isinstance(watched, QWidget) else None
        )
        blocked_control = isinstance(child, (QAbstractButton, QComboBox, QLineEdit))
        if (
            self._closed
            or self.pending
            or not self._available()
            or not sources
            or blocked_control
            or not event.possibleActions() & Qt.DropAction.CopyAction
        ):
            self.clear_highlight()
            event.ignore()
            return True
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        if isinstance(event, QDropEvent) and not isinstance(event, QDragMoveEvent):
            self.clear_highlight()
            self.pending = True
            self._sources = sources
            self._timer.start(0)
        else:
            self._highlight.setProperty("dragActive", True)
            self._highlight.style().unpolish(self._highlight)
            self._highlight.style().polish(self._highlight)
            self._highlight.update()
        return True
