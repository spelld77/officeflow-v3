"""Small read-only viewer pieces that never mutate tasks or record drafts."""

from collections.abc import Callable
from datetime import UTC, datetime

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QResizeEvent, QTextLayout, QTextOption
from PySide6.QtWidgets import QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from officeflow.domain.task import Task
from officeflow.presentation.compact_labels import CompactLabel
from officeflow.presentation.schedule_help import schedule_help


class ScheduleLabel(CompactLabel):
    def __init__(
        self, parent: QWidget | None = None, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.task: Task | None = None
        self.confirmed = True
        self.clock = clock or (lambda: datetime.now(UTC))
        super().__init__(parent=parent)

    def event(self, event: QEvent) -> bool:
        if event.type() is QEvent.Type.ToolTip and self.task is not None:
            self.setToolTip(schedule_help(self.task, self.clock(), confirmed=self.confirmed))
        return super().event(event)


class TaskDescription(QWidget):
    def __init__(self, parent: QWidget | None = None, *, collapsed: bool = False) -> None:
        super().__init__(parent)
        self._mode = "collapsed" if collapsed else "preview"
        self._text = ""
        self._measuring = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.body = QPlainTextEdit(self)
        self.body.setObjectName("taskDescription")
        self.body.setReadOnly(True)
        self.body.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.document().setDocumentMargin(2)
        self.body.setAccessibleName("업무 설명 (읽기 전용)")
        layout.addWidget(self.body)
        self.toggle = QPushButton(self)
        self.toggle.setObjectName("taskDescriptionToggle")
        self.toggle.clicked.connect(self._toggle)
        row = QHBoxLayout()
        row.addWidget(self.toggle)
        row.addStretch()
        layout.addLayout(row)

    @property
    def collapsed(self) -> bool:
        return self._mode == "collapsed"

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.body.setPlainText(text)
        self.setVisible(bool(text.strip()))
        self._measure()

    def set_collapsed(self, value: bool) -> None:
        self._mode = "collapsed" if value else "preview"
        self._measure()

    def _wrapped_lines(self) -> int:
        width = max(20, self.body.viewport().width() - 8)
        count = 0
        for text in self._text.split("\n"):
            if not text:
                count += 1
            else:
                paragraph = QTextLayout(text, self.body.font())
                option = QTextOption()
                option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
                paragraph.setTextOption(option)
                paragraph.beginLayout()
                while count < 7:
                    line = paragraph.createLine()
                    if not line.isValid():
                        break
                    line.setLineWidth(width)
                    count += 1
                paragraph.endLayout()
            if count >= 7:
                break
        return max(1, count)

    def _measure(self) -> None:
        if self._measuring:
            return
        self._measuring = True
        try:
            self.body.setVisible(not self.collapsed)
            lines = self._wrapped_lines()
            limit = 6 if self._mode == "expanded" else 3
            self.body.setFixedHeight(min(lines, limit) * self.body.fontMetrics().lineSpacing() + 16)
            self.body.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded
                if self._mode == "expanded"
                else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            self.toggle.setText(
                "설명 보기"
                if self.collapsed
                else "더 보기"
                if self._mode == "preview" and lines > 3
                else "설명 접기"
            )
        finally:
            self._measuring = False

    def _toggle(self) -> None:
        self._mode = (
            "preview"
            if self.collapsed
            else "expanded"
            if self._mode == "preview" and self._wrapped_lines() > 3
            else "collapsed"
        )
        self._measure()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._measure()
