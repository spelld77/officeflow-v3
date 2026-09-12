from __future__ import annotations

from pytestqt.qtbot import QtBot

from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow


def test_main_window_has_phase_one_shell(qtbot: QtBot) -> None:
    window = MainWindow(AppSettings())
    qtbot.addWidget(window)
    window.show()

    assert window.windowTitle() == "OfficeFlow v3"
    assert window.minimumWidth() == 960
    assert window.findChild(type(window.centralWidget()), "appRoot") is not None
