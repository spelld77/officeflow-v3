from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QCloseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
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
    DETAIL_BREAKPOINT = 1100
    COMPACT_BREAKPOINT = 850
    MINIMUM_WIDTH = 760
    MINIMUM_HEIGHT = 560

    def __init__(
        self,
        settings: AppSettings,
        save_settings: Callable[[AppSettings], None] | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._save_settings = save_settings
        self._compact_navigation = False
        self._compact_summaries = False
        self._nav_buttons: list[QPushButton] = []
        self._summary_frames: list[QFrame] = []

        self.setWindowTitle("OfficeFlow v3")
        self.resize(settings.window_width, settings.window_height)
        self.setMinimumSize(self.MINIMUM_WIDTH, self.MINIMUM_HEIGHT)
        self.setStyleSheet(LIGHT_STYLESHEET)

        root = QWidget()
        root.setObjectName("appRoot")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._sidebar = self._build_sidebar()
        layout.addWidget(self._sidebar)
        layout.addWidget(self._build_workspace(), 1)
        self.setCentralWidget(root)
        self._restore_window_position(settings)
        self._apply_responsive_layout()

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(212)
        self._sidebar_layout = QVBoxLayout(sidebar)
        self._sidebar_layout.setContentsMargins(18, 24, 18, 20)
        self._sidebar_layout.setSpacing(6)

        self._brand_title = self._named_label("OfficeFlow", "brandTitle")
        self._brand_caption = self._named_label("나의 업무 흐름", "brandCaption")
        self._sidebar_layout.addWidget(self._brand_title)
        self._sidebar_layout.addWidget(self._brand_caption)
        self._sidebar_layout.addSpacing(22)

        for index, label in enumerate(("오늘", "예정", "중요", "대기", "완료", "전체 업무")):
            self._sidebar_layout.addWidget(self._create_nav_button(label, index == 0))

        self._sidebar_layout.addSpacing(16)
        for label in ("캘린더", "업무일지", "설정"):
            self._sidebar_layout.addWidget(self._create_nav_button(label))

        self._sidebar_layout.addStretch()
        self._version_label = self._named_label("v3.0 · 설계 기반", "brandCaption")
        self._sidebar_layout.addWidget(self._version_label)
        return sidebar

    def _build_workspace(self) -> QWidget:
        workspace = QWidget()
        self._workspace_layout = QVBoxLayout(workspace)
        self._workspace_layout.setContentsMargins(24, 20, 24, 24)
        self._workspace_layout.setSpacing(16)
        self._workspace_layout.addWidget(self._build_top_bar())

        self._body_layout = QHBoxLayout()
        self._body_layout.setSpacing(16)
        self._body_layout.addWidget(self._build_content(), 3)
        self._detail_panel = self._build_detail()
        self._body_layout.addWidget(self._detail_panel, 2)
        self._workspace_layout.addLayout(self._body_layout, 1)
        return workspace

    def _build_top_bar(self) -> QWidget:
        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        layout = QHBoxLayout(top_bar)
        layout.setContentsMargins(14, 12, 14, 12)

        self._search = QLineEdit()
        self._search.setPlaceholderText("업무, 내용, 업무일지 검색  (Ctrl+K)")
        self._search.setClearButtonEnabled(True)
        self._search.setMaximumWidth(520)
        layout.addWidget(self._search, 1)
        layout.addStretch()

        self._add_button = QPushButton("+ 새 업무")
        self._add_button.setObjectName("primaryButton")
        self._add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self._add_button)
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
        page_caption = self._named_label("지금 집중할 업무를 먼저 보여드립니다.", "mutedText")
        page_caption.setWordWrap(True)
        titles.addWidget(page_caption)
        heading.addLayout(titles)
        heading.addStretch()
        filter_button = QPushButton("필터  ▾")
        heading.addWidget(filter_button)
        layout.addLayout(heading)

        self._summary_layout = QGridLayout()
        self._summary_layout.setSpacing(10)
        for name, value in (("지연", "0"), ("진행 중", "0"), ("오늘", "0"), ("완료", "0")):
            frame = QFrame()
            frame.setProperty("summary", True)
            item_layout = QVBoxLayout(frame)
            item_layout.setContentsMargins(14, 11, 14, 11)
            count = QLabel(value)
            count.setProperty("count", True)
            item_layout.addWidget(count)
            item_layout.addWidget(self._named_label(name, "mutedText"))
            self._summary_frames.append(frame)
        self._arrange_summary_cards(compact=False)
        layout.addLayout(self._summary_layout)

        empty = QFrame()
        empty_layout = QVBoxLayout(empty)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(
            QLabel("아직 등록된 업무가 없습니다."), alignment=Qt.AlignmentFlag.AlignCenter
        )
        empty_description = self._named_label(
            "새 업무를 등록하면 중요도와 일정에 따라 자동으로 정리됩니다.",
            "mutedText",
        )
        empty_description.setWordWrap(True)
        empty_description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_description.setMaximumWidth(520)
        empty_layout.addWidget(empty_description, alignment=Qt.AlignmentFlag.AlignCenter)
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

    def _create_nav_button(self, label: str, selected: bool = False) -> QPushButton:
        button = QPushButton(label)
        button.setProperty("nav", True)
        button.setProperty("selected", selected)
        button.setProperty("fullLabel", label)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._nav_buttons.append(button)
        return button

    def _apply_responsive_layout(self) -> None:
        width = self.width()
        compact_navigation = width < self.COMPACT_BREAKPOINT
        show_detail = width >= self.DETAIL_BREAKPOINT

        self._detail_panel.setVisible(show_detail)
        self._body_layout.setSpacing(16 if show_detail else 0)

        if compact_navigation:
            self._sidebar.setFixedWidth(88)
            self._sidebar_layout.setContentsMargins(10, 20, 10, 16)
            self._workspace_layout.setContentsMargins(12, 12, 12, 14)
            self._brand_title.setText("OF")
            self._brand_caption.hide()
            self._version_label.setText("v3")
            self._search.setPlaceholderText("업무 검색")
            self._add_button.setText("+ 업무")
        else:
            self._sidebar.setFixedWidth(212 if show_detail else 180)
            self._sidebar_layout.setContentsMargins(18, 24, 18, 20)
            margin = 24 if show_detail else 18
            self._workspace_layout.setContentsMargins(margin, 18, margin, 20)
            self._brand_title.setText("OfficeFlow")
            self._brand_caption.show()
            self._version_label.setText("v3.0 · 설계 기반")
            self._search.setPlaceholderText("업무, 내용, 업무일지 검색  (Ctrl+K)")
            self._add_button.setText("+ 새 업무")

        if compact_navigation != self._compact_navigation:
            self._compact_navigation = compact_navigation
            compact_labels = {
                "전체 업무": "전체",
                "캘린더": "달력",
                "업무일지": "일지",
            }
            for button in self._nav_buttons:
                full_label = str(button.property("fullLabel"))
                button.setText(
                    compact_labels.get(full_label, full_label) if compact_navigation else full_label
                )
                button.setProperty("compact", compact_navigation)
                button.style().unpolish(button)
                button.style().polish(button)

        if compact_navigation != self._compact_summaries:
            self._arrange_summary_cards(compact=compact_navigation)

    def _arrange_summary_cards(self, *, compact: bool) -> None:
        for frame in self._summary_frames:
            self._summary_layout.removeWidget(frame)
        columns = 2 if compact else 4
        for index, frame in enumerate(self._summary_frames):
            self._summary_layout.addWidget(frame, index // columns, index % columns)
        self._compact_summaries = compact

    def _restore_window_position(self, settings: AppSettings) -> None:
        if settings.window_x is None or settings.window_y is None:
            return
        candidate = self.frameGeometry()
        candidate.moveTopLeft(QPoint(settings.window_x, settings.window_y))
        if any(
            screen.availableGeometry().intersects(candidate) for screen in QApplication.screens()
        ):
            self.move(settings.window_x, settings.window_y)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_detail_panel"):
            self._apply_responsive_layout()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._save_settings is not None:
            geometry = self.normalGeometry()
            self._settings = replace(
                self._settings,
                window_width=max(self.MINIMUM_WIDTH, geometry.width()),
                window_height=max(self.MINIMUM_HEIGHT, geometry.height()),
                window_x=geometry.x(),
                window_y=geometry.y(),
            )
            self._save_settings(self._settings)
        super().closeEvent(event)

    @staticmethod
    def _named_label(text: str, object_name: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(object_name)
        return label
