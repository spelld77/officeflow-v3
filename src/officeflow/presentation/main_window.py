from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.theme import LIGHT_STYLESHEET


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self.setWindowTitle("OfficeFlow v3")
        self.resize(settings.window_width, settings.window_height)
        self.setMinimumSize(960, 640)
        self.setStyleSheet(LIGHT_STYLESHEET)

        root = QWidget()
        root.setObjectName("appRoot")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())
        layout.addWidget(self._build_workspace(), 1)
        self.setCentralWidget(root)

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(212)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 24, 18, 20)
        layout.setSpacing(6)

        title = self._named_label("OfficeFlow", "brandTitle")
        caption = self._named_label("나의 업무 흐름", "brandCaption")
        layout.addWidget(title)
        layout.addWidget(caption)
        layout.addSpacing(22)

        for index, label in enumerate(("오늘", "예정", "중요", "대기", "완료", "전체 업무")):
            button = QPushButton(label)
            button.setProperty("nav", True)
            button.setProperty("selected", index == 0)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            layout.addWidget(button)

        layout.addSpacing(16)
        for label in ("캘린더", "업무일지", "설정"):
            button = QPushButton(label)
            button.setProperty("nav", True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            layout.addWidget(button)

        layout.addStretch()
        version = self._named_label("v3.0 · 설계 기반", "brandCaption")
        layout.addWidget(version)
        return sidebar

    def _build_workspace(self) -> QWidget:
        workspace = QWidget()
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(self._build_top_bar())

        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(self._build_content(), 3)
        body.addWidget(self._build_detail(), 2)
        layout.addLayout(body, 1)
        return workspace

    def _build_top_bar(self) -> QWidget:
        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        layout = QHBoxLayout(top_bar)
        layout.setContentsMargins(14, 12, 14, 12)

        search = QLineEdit()
        search.setPlaceholderText("업무, 내용, 업무일지 검색  (Ctrl+K)")
        search.setClearButtonEnabled(True)
        search.setMaximumWidth(520)
        layout.addWidget(search, 1)
        layout.addStretch()

        add_button = QPushButton("+ 새 업무")
        add_button.setObjectName("primaryButton")
        add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(add_button)
        return top_bar

    def _build_content(self) -> QWidget:
        card = QFrame()
        card.setObjectName("contentCard")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(16)

        heading = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(self._named_label("오늘", "pageTitle"))
        titles.addWidget(self._named_label("지금 집중할 업무를 먼저 보여드립니다.", "mutedText"))
        heading.addLayout(titles)
        heading.addStretch()
        filter_button = QPushButton("필터  ▾")
        heading.addWidget(filter_button)
        layout.addLayout(heading)

        summaries = QHBoxLayout()
        summaries.setSpacing(10)
        for name, value in (("지연", "0"), ("진행 중", "0"), ("오늘", "0"), ("완료", "0")):
            frame = QFrame()
            frame.setProperty("summary", True)
            item_layout = QVBoxLayout(frame)
            item_layout.setContentsMargins(14, 11, 14, 11)
            count = QLabel(value)
            count.setProperty("count", True)
            item_layout.addWidget(count)
            item_layout.addWidget(self._named_label(name, "mutedText"))
            summaries.addWidget(frame)
        layout.addLayout(summaries)

        empty = QFrame()
        empty_layout = QVBoxLayout(empty)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(
            QLabel("아직 등록된 업무가 없습니다."), alignment=Qt.AlignmentFlag.AlignCenter
        )
        empty_layout.addWidget(
            self._named_label(
                "새 업무를 등록하면 중요도와 일정에 따라 자동으로 정리됩니다.",
                "mutedText",
            ),
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        layout.addWidget(empty, 1)
        return card

    def _build_detail(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("detailPanel")
        panel.setMinimumWidth(290)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(10)
        layout.addWidget(self._named_label("업무 상세", "pageTitle"))
        layout.addWidget(self._named_label("목록에서 업무를 선택하세요.", "mutedText"))
        layout.addStretch()
        return panel

    @staticmethod
    def _named_label(text: str, object_name: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(object_name)
        return label
