# ruff: noqa: F811
from pathlib import Path

import pytest

from officeflow.presentation.folder_links import FolderLinksPanel
from tests.ui.test_folder_and_task_archive_ui import files, link_service  # noqa: F401


@pytest.mark.parametrize("choice", ["use", "cancel", "changed", "close", "other"])
def test_folder_candidate_requires_choice_before_save_and_open(
    files, tmp_path, qtbot, monkeypatch, choice, qapp, request
):
    import os

    from officeflow.presentation import folder_links

    if os.getenv("OFFICEFLOW_U43_CAPTURE") == "1":
        from PySide6.QtGui import QColor, QPalette

        previous = qapp.palette()
        request.addfinalizer(lambda: qapp.setPalette(previous))
        if choice == "cancel":
            palette = QPalette(previous)
            for role in (
                QPalette.ColorRole.Window,
                QPalette.ColorRole.Base,
                QPalette.ColorRole.Button,
            ):
                palette.setColor(role, QColor("#202020"))
            qapp.setPalette(palette)
    service = link_service(files)
    old = tmp_path / "01.폴더_a"
    old.mkdir()
    link = service.save(files[1].id, old.name, old)
    new = tmp_path / "04.폴더_a"
    old.rename(new)
    panel = FolderLinksPanel(service, link.task_id)
    qtbot.addWidget(panel)
    panel.resize(500, 400)
    panel.show()
    opened = []
    monkeypatch.setattr(
        folder_links.QDesktopServices,
        "openUrl",
        lambda url: opened.append(Path(url.toLocalFile())) or True,
    )
    panel.open_selected()
    qtbot.waitUntil(lambda: panel._choice_dialog is not None, timeout=8000)
    dialog = panel._choice_dialog
    assert service.list(link.task_id)[0] == link and not opened
    assert {button.text() for button in dialog.buttons()} == {
        "이 폴더 사용",
        "다른 폴더 선택",
        "취소",
    }
    if os.getenv("OFFICEFLOW_U43_CAPTURE") == "1":
        qtbot.wait(30)
        output = Path("artifacts/u43-folders/captures")
        output.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(output / f"candidate-{choice}.png"))
    if choice == "cancel":
        next(button for button in dialog.buttons() if button.text() == "취소").click()
    elif choice == "other":
        calls = []
        monkeypatch.setattr(
            folder_links.QFileDialog, "getExistingDirectory", lambda *_: calls.append(True) or ""
        )
        next(button for button in dialog.buttons() if button.text() == "다른 폴더 선택").click()
        assert calls
    elif choice == "close":
        panel.shutdown()
    else:
        if choice == "changed":
            new.rename(tmp_path / "another")
        next(button for button in dialog.buttons() if button.text() == "이 폴더 사용").click()
    qtbot.waitUntil(lambda: panel._job_thread is None, timeout=8000)
    if choice == "use":
        assert opened == [new]
        assert service.list(link.task_id)[0].path == str(new)
    else:
        assert not opened and service.list(link.task_id)[0] == link
    panel.shutdown()


def test_readonly_candidate_keeps_link_and_does_not_offer_update(files, tmp_path, qtbot):
    service = link_service(files)
    old = tmp_path / "old"
    old.mkdir()
    link = service.save(files[1].id, old.name, old)
    old.rename(tmp_path / "new")
    panel = FolderLinksPanel(service, link.task_id, read_only=True)
    qtbot.addWidget(panel)
    panel.open_selected()
    qtbot.waitUntil(lambda: panel._job_thread is None, timeout=8000)
    assert panel._choice_dialog is None
    assert "복원한 뒤" in panel.status.text()
    assert service.list(link.task_id)[0] == link
    panel.shutdown()
