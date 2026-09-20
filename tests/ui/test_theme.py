from __future__ import annotations

from PySide6.QtGui import QAction, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCalendarWidget,
    QListWidget,
    QMenu,
    QWidget,
)
from pytestqt.qtbot import QtBot

from officeflow.presentation.theme import LIGHT_STYLESHEET


def test_light_theme_keeps_calendar_and_popup_menu_readable(qtbot: QtBot) -> None:
    parent = QWidget()
    parent.setStyleSheet(LIGHT_STYLESHEET)
    qtbot.addWidget(parent)

    calendar = QCalendarWidget(parent)
    menu = QMenu(parent)
    menu.addAction(QAction("OfficeFlow 열기", menu))
    calendar.ensurePolished()
    menu.ensurePolished()

    calendar_view = calendar.findChild(QAbstractItemView)
    assert calendar_view is not None
    assert calendar_view.palette().color(QPalette.ColorRole.Base).name() == "#ffffff"
    assert calendar_view.palette().color(QPalette.ColorRole.Text).name() == "#172033"
    assert menu.palette().color(QPalette.ColorRole.Window).name() == "#ffffff"
    assert menu.palette().color(QPalette.ColorRole.WindowText).name() == "#172033"


def test_light_theme_keeps_attachment_cleanup_list_readable(qtbot: QtBot) -> None:
    parent = QWidget()
    parent.setStyleSheet(LIGHT_STYLESHEET)
    qtbot.addWidget(parent)

    cleanup_list = QListWidget(parent)
    cleanup_list.setObjectName("attachmentCleanupList")
    cleanup_list.addItem("복원 가능 · 보고서.pdf · 1.2 MB")
    cleanup_list.ensurePolished()

    palette = cleanup_list.palette()
    assert palette.color(QPalette.ColorRole.Base).name() == "#ffffff"
    assert palette.color(QPalette.ColorRole.Text).name() == "#172033"
    assert palette.color(QPalette.ColorRole.Highlight).name() == "#dfe9ff"
    assert palette.color(QPalette.ColorRole.HighlightedText).name() == "#172033"
