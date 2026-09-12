from __future__ import annotations

from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow


def test_main_window_has_phase_one_shell(qtbot: QtBot) -> None:
    window = MainWindow(AppSettings())
    qtbot.addWidget(window)
    window.show()

    assert window.windowTitle() == "OfficeFlow v3"
    assert window.minimumWidth() == 760
    assert window.findChild(type(window.centralWidget()), "appRoot") is not None


def test_wide_window_shows_three_panels(qtbot: QtBot) -> None:
    window = MainWindow(AppSettings())
    qtbot.addWidget(window)
    window.resize(1280, 800)
    window.show()
    qtbot.wait(10)

    detail = window.findChild(QWidget, "detailPanel")
    sidebar = window.findChild(QWidget, "sidebar")
    assert detail is not None and detail.isVisible()
    assert sidebar is not None and sidebar.width() == 212


def test_medium_window_prioritizes_task_list(qtbot: QtBot) -> None:
    window = MainWindow(AppSettings())
    qtbot.addWidget(window)
    window.resize(960, 640)
    window.show()
    qtbot.wait(10)

    detail = window.findChild(QWidget, "detailPanel")
    sidebar = window.findChild(QWidget, "sidebar")
    assert detail is not None and detail.isHidden()
    assert sidebar is not None and sidebar.width() == 180
    assert window._summary_layout.getItemPosition(3)[:2] == (0, 3)


def test_compact_window_uses_small_navigation_and_wrapped_summaries(qtbot: QtBot) -> None:
    window = MainWindow(AppSettings())
    qtbot.addWidget(window)
    window.resize(760, 560)
    window.show()
    qtbot.wait(10)

    sidebar = window.findChild(QWidget, "sidebar")
    detail = window.findChild(QWidget, "detailPanel")
    assert sidebar is not None and sidebar.width() == 88
    assert detail is not None and detail.isHidden()
    assert window._summary_layout.getItemPosition(2)[:2] == (1, 0)


def test_window_geometry_is_saved_on_close(qtbot: QtBot) -> None:
    saved: list[AppSettings] = []
    window = MainWindow(AppSettings(), save_settings=saved.append)
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
