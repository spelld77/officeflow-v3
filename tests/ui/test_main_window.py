from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit, QListView, QPushButton, QWidget
from pytestqt.qtbot import QtBot

from officeflow.application.tasks import TaskService
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow


def test_main_window_has_phase_two_shell(qtbot: QtBot, task_service: TaskService) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.show()

    assert window.windowTitle() == "OfficeFlow v3"
    assert window.minimumWidth() == 760
    assert window.findChild(type(window.centralWidget()), "appRoot") is not None


def test_wide_window_shows_three_panels(qtbot: QtBot, task_service: TaskService) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.resize(1280, 800)
    window.show()
    qtbot.wait(10)

    detail = window.findChild(QWidget, "detailPanel")
    sidebar = window.findChild(QWidget, "sidebar")
    assert detail is not None and detail.isVisible()
    assert sidebar is not None and sidebar.width() == 212


def test_medium_window_prioritizes_task_list(qtbot: QtBot, task_service: TaskService) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.resize(960, 640)
    window.show()
    qtbot.wait(10)

    detail = window.findChild(QWidget, "detailPanel")
    sidebar = window.findChild(QWidget, "sidebar")
    assert detail is not None and detail.isHidden()
    assert sidebar is not None and sidebar.width() == 180
    assert window._summary_layout.getItemPosition(3)[:2] == (0, 3)


def test_compact_window_uses_small_navigation_and_wrapped_summaries(
    qtbot: QtBot, task_service: TaskService
) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.resize(760, 560)
    window.show()
    qtbot.wait(10)

    sidebar = window.findChild(QWidget, "sidebar")
    detail = window.findChild(QWidget, "detailPanel")
    assert sidebar is not None and sidebar.width() == 88
    assert detail is not None and detail.isHidden()
    assert window._summary_layout.getItemPosition(2)[:2] == (1, 0)


def test_window_geometry_is_saved_on_close(qtbot: QtBot, task_service: TaskService) -> None:
    saved: list[AppSettings] = []
    window = MainWindow(AppSettings(), task_service, save_settings=saved.append)
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    qtbot.wait(10)

    window.close()

    assert saved
    assert saved[-1].window_width == 900
    assert saved[-1].window_height == 600
    assert saved[-1].window_x is not None
    assert saved[-1].window_y is not None


def test_quick_add_creates_today_task(qtbot: QtBot, task_service: TaskService) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.show()

    quick_add = window.findChild(QLineEdit, "quickAddEdit")
    assert quick_add is not None
    qtbot.keyClicks(quick_add, "Write phase two tests")
    qtbot.keyPress(quick_add, Qt.Key.Key_Return)

    task_list = window.findChild(QListView, "taskList")
    assert task_list is not None
    assert task_list.model().rowCount() == 1


def test_tab_moves_from_quick_add_input_to_add_button(
    qtbot: QtBot, task_service: TaskService
) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.show()
    quick_add = window.findChild(QLineEdit, "quickAddEdit")
    quick_add_button = window.findChild(QPushButton, "quickAddButton")
    assert quick_add is not None
    assert quick_add_button is not None
    quick_add.setFocus()

    qtbot.keyPress(quick_add, Qt.Key.Key_Tab)

    assert window.focusWidget() is quick_add_button
