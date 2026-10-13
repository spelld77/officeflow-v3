"""Presentation-only attachment filtering, summaries and coalesced source reads."""

from collections.abc import Callable
from typing import cast

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget

from officeflow.infrastructure.database.document_catalog import AttachmentSourceSummary
from officeflow.presentation.background import finish_thread


class AttachmentDetailLabel(QLabel):
    """Routine metadata is one line; operation notices remain ordinary readable text."""

    def __init__(self) -> None:
        super().__init__()
        self.full_text = ""
        self.is_summary = False
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)

    def set_summary(self, text: str) -> None:
        self.is_summary = True
        self.full_text = text
        self.setWordWrap(False)
        self.setMaximumHeight(self.fontMetrics().lineSpacing() + 8)
        self.setToolTip(text)
        self.setAccessibleName(text)
        self._render_summary()

    def _render_summary(self) -> None:
        super().setText(
            self.fontMetrics().elidedText(
                self.full_text, Qt.TextElideMode.ElideMiddle, max(1, self.contentsRect().width())
            )
        )

    def setText(self, text: str) -> None:
        self.is_summary = False
        self.full_text = text
        self.setWordWrap(True)
        self.setMaximumHeight(16_777_215)
        self.setToolTip(text)
        self.setAccessibleName(text)
        super().setText(text)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self.is_summary:
            self._render_summary()


class _SourceWorker(QObject):
    requested = Signal(object)
    completed = Signal(object)

    def __init__(self, lookup: Callable[[int, int], AttachmentSourceSummary]) -> None:
        super().__init__()
        self.lookup = lookup
        self.requested.connect(self.run, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def run(self, value: object) -> None:
        token = cast(tuple[int, int, int, int], value)
        try:
            result = self.lookup(token[0], token[1])
        except Exception:
            result = AttachmentSourceSummary(state="unavailable")
        self.completed.emit((token, result))


class AttachmentSourceController(QObject):
    updated = Signal()
    informationReady = Signal(object)

    def __init__(
        self, lookup: Callable[[int, int], AttachmentSourceSummary], parent: QWidget
    ) -> None:
        super().__init__(parent)
        self.owner = parent
        self.lookup = lookup
        self._thread: QThread | None = None
        self._worker: _SourceWorker | None = None
        self._current: tuple[int, int, int, int] | None = None
        self._running: tuple[int, int, int, int] | None = None
        self._pending: tuple[int, int, int, int] | None = None
        self._information: tuple[int, int, int, int] | None = None
        self.result: AttachmentSourceSummary | None = None
        self._closed = False

    @property
    def current_token(self) -> tuple[int, int, int, int] | None:
        return self._current

    def keep_information_request(self) -> None:
        """One explicit information read survives rapid list selection changes."""
        self._information = self._current if self.result is None else None

    def invalidate(self) -> None:
        self._current = None
        self._pending = None
        self.result = None

    def request(
        self, task: int, identity: int, revision: int, generation: int
    ) -> AttachmentSourceSummary | None:
        if self._closed:
            return None
        token = (task, identity, revision, generation)
        if self._current == token:
            return self.result
        self._current, self.result = token, None
        if self._running is not None:
            self._pending = token
        else:
            self._start(token)
        return None

    def _start(self, token: tuple[int, int, int, int]) -> None:
        if self._thread is None:
            self._thread = QThread(self)
            self._worker = _SourceWorker(self.lookup)
            self._worker.moveToThread(self._thread)
            self._worker.completed.connect(self._completed, Qt.ConnectionType.QueuedConnection)
            self._thread.finished.connect(self._worker.deleteLater)
            self._thread.start()
        self._running = token
        assert self._worker is not None
        self._worker.requested.emit(token)

    @Slot(object)
    def _completed(self, value: object) -> None:
        token, result = cast(tuple[tuple[int, int, int, int], AttachmentSourceSummary], value)
        self._running = None
        if not self._closed and token == self._information:
            self._information = None
            self.informationReady.emit((token, result))
        if not self._closed and token == self._current:
            self.result = result
            self.updated.emit()
        if self._closed:
            return
        if self._information is not None:
            self._start(self._information)
        else:
            pending, self._pending = self._pending, None
            if pending is not None and pending == self._current and pending != token:
                self._start(pending)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.invalidate()
        self._information = None
        if self._thread is not None:
            self._thread.quit()
            finish_thread(
                self._thread, parent=self.owner, label="첨부 출처 조회를 마무리하고 있습니다…"
            )
            self._thread.deleteLater()
        self._thread = None
        self._worker = None
