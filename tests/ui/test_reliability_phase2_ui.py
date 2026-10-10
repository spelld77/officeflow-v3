from __future__ import annotations

from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPalette

from officeflow.application.attachment_search import (
    AttachmentSearchHit,
    AttachmentSearchPage,
    AttachmentSearchService,
)
from officeflow.domain.enums import TaskStatus
from officeflow.infrastructure.backup_files import BackupFileSession
from officeflow.presentation.attachment_search_page import AttachmentSearchPageWidget
from officeflow.presentation.backup_files_dialog import BackupFilesDialog
from officeflow.presentation.theme import LIGHT_STYLESHEET
from tests.integration.test_reliability_phase2 import one_backup


def wait(dialog, qtbot):
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=10000)


def test_backup_dialog_selection_save_and_close(qtbot, tmp_path):
    manager, archive = one_backup(tmp_path)
    dialog = BackupFilesDialog(manager)
    qtbot.addWidget(dialog)
    dialog.resize(500, 420)
    dialog.show()
    dialog.load(archive)
    wait(dialog, qtbot)
    assert dialog.list.count() == 1
    assert not dialog.save_button.isEnabled()
    dialog.list.item(0).setSelected(True)
    assert dialog.selected_ids() == (1,)
    assert dialog.save_button.isEnabled()
    folder = tmp_path / "external"
    folder.mkdir()
    dialog.export((1,), folder)
    wait(dialog, qtbot)
    assert "저장 1" in dialog.status.text()
    assert dialog.folder_button.isVisible()
    assert dialog.rect().contains(
        dialog.folder_button.mapTo(dialog, dialog.folder_button.rect().bottomRight())
    )
    assert dialog.list.count() == 1 and dialog.selected_ids() == ()
    assert (folder / "서식.txt").read_bytes() == b"data"
    assert not manager._paths.pending_restore_file.exists()
    dialog.refresh()
    wait(dialog, qtbot)
    assert dialog.list.item(0).data(Qt.ItemDataRole.UserRole).kind == "attachment"
    temporary = dialog.session.root
    for widget in (
        dialog.save_button,
        dialog.close_button,
        dialog.search_button,
        dialog.next_button,
    ):
        assert dialog.rect().contains(widget.mapTo(dialog, widget.rect().bottomRight()))
    dialog.reject()
    assert not temporary.exists()


def test_close_during_worker_keeps_ui_responsive_and_joins(qtbot, tmp_path, monkeypatch):
    manager, archive = one_backup(tmp_path)
    started = Event()

    def slow(self, cancel):
        started.set()
        while not cancel():
            Event().wait(0.01)
        from officeflow.infrastructure.backup import BackupCanceledError

        raise BackupCanceledError("취소")

    monkeypatch.setattr(BackupFileSession, "load", slow)
    dialog = BackupFilesDialog(manager)
    qtbot.addWidget(dialog)
    beats = []
    timer = QTimer(dialog)
    timer.timeout.connect(lambda: beats.append(1))
    timer.start(10)
    dialog.load(archive)
    qtbot.waitUntil(started.is_set)
    qtbot.wait(70)
    assert beats
    thread = dialog._thread
    dialog.reject()
    assert dialog._thread is None and dialog.session is None
    from shiboken6 import isValid

    assert not isValid(thread) or not thread.isRunning()


def test_search_kind_filter_keeps_identity_kind_and_details(qtbot):
    from datetime import UTC, datetime

    class Repository:
        def search_page(self, query, *, cancel_requested=None):
            hit = AttachmentSearchHit(
                1,
                1,
                "업무",
                TaskStatus.ACTIVE,
                "자료.txt",
                1,
                datetime.now(UTC),
                False,
                False,
                False,
                source_kind="version",
                label="제출 전",
                memo="검토",
            )
            return AttachmentSearchPage((hit,), None, False)

    page = AttachmentSearchPageWidget(AttachmentSearchService(Repository()), "Asia/Seoul")
    page.setStyleSheet(LIGHT_STYLESHEET)
    qtbot.addWidget(page)
    page.show()
    page.activate("제출")
    qtbot.waitUntil(lambda: page._worker is None and bool(page.model.items))
    page.list_view.setCurrentIndex(page.model.index(0, 0))
    assert page._selected_id == ("version", 1)
    assert page.manage_button.text() == "보관 자료"
    assert page.selected_hit().reference.kind.value == "version"
    page.source_combo.setCurrentIndex(2)
    assert page._query().source_kind == "version"
    page.shutdown()


def test_backup_empty_failed_load_has_message_and_no_actions(qtbot, tmp_path):
    manager, _ = one_backup(tmp_path)
    invalid = tmp_path / "bad"
    invalid.write_bytes(b"not a zip")
    dialog = BackupFilesDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.load(invalid)
    wait(dialog, qtbot)
    assert dialog.status.text()
    assert dialog.list.count() == 0 and not dialog.save_button.isEnabled()
    dialog.reject()


def test_backup_small_dark_palette_readable(qtbot, qapp, tmp_path):
    manager, archive = one_backup(tmp_path)
    saved = qapp.palette()
    dark = QPalette(saved)
    for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
        dark.setColor(role, QColor("#202020"))
    qapp.setPalette(dark)
    dialog = BackupFilesDialog(manager)
    qtbot.addWidget(dialog)
    try:
        dialog.resize(500, 420)
        dialog.show()
        dialog.load(archive)
        wait(dialog, qtbot)
        image = dialog.list.viewport().grab().toImage()
        assert image.pixelColor(2, image.height() - 2).lightness() > 180
        for button in (dialog.save_button, dialog.search_button, dialog.close_button):
            assert dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight()))
        assert dialog.list.height() >= 80
    finally:
        dialog.reject()
        qapp.setPalette(saved)


@pytest.mark.parametrize("scale", ["1", "1.5", "2"])
@pytest.mark.parametrize("palette", ["light", "dark"])
def test_phase2_visual_child(scale, palette):
    import os
    import subprocess
    import sys

    output = (
        Path(__file__).resolve().parents[2]
        / "artifacts"
        / "reliability-phase2-visual"
        / f"{palette}-{scale}"
    )
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.ui.reliability_phase2_visual_child",
            str(output),
            scale,
            palette,
        ],
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        capture_output=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr.decode("utf-8", errors="replace")


@pytest.mark.parametrize("kind", ["attachment", "version", "snapshot_file"])
def test_main_search_routes_exact_reference_not_same_numeric_attachment(
    qtbot, tmp_path, monkeypatch, kind
):
    from PySide6.QtGui import QDesktopServices

    from officeflow.application.attachment_export import AttachmentExportService
    from officeflow.application.attachment_search import AttachmentSearchQuery
    from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
    from officeflow.infrastructure.settings.store import AppSettings
    from officeflow.presentation.main_window import MainWindow
    from tests.integration.test_document_lifecycle import lifecycle
    from tests.integration.test_reliability_phase2 import populate

    with lifecycle(tmp_path) as (env, facade):
        populate(env, facade.documents)
        exports = AttachmentExportService(
            env.attachments, ExternalExportStorage(tmp_path / "journals")
        )
        window = MainWindow(
            AppSettings(automatic_backup_enabled=False),
            env.tasks,
            record_service=env.records,
            attachment_service=env.attachments,
            attachment_search_service=env.search,
            attachment_export_service=exports,
        )
        qtbot.addWidget(window)
        window.show()
        hit = env.search.search_page(AttachmentSearchQuery(source_kind=kind)).items[0]
        chosen = []
        monkeypatch.setattr(window._attachment_export_controller, "choose", chosen.append)
        window._save_search_file(hit)
        assert chosen == [(hit.reference,)]
        opened = []
        monkeypatch.setattr(
            QDesktopServices, "openUrl", lambda url: opened.append(Path(url.toLocalFile())) or True
        )
        window._open_search_file(hit)
        qtbot.waitUntil(lambda: bool(opened), timeout=10000)
        if kind != "attachment":
            fixed = facade.catalog.get(kind, hit.attachment_id).file
            assert opened[0] != env.attachments.storage_root / fixed.relative_path
        assert opened[0].read_bytes() == b"current bytes"
        window.shutdown()


def test_version_manage_search_focuses_old_hit_without_full_list(qtbot, tmp_path):
    from officeflow.application.attachment_export import AttachmentExportService
    from officeflow.application.attachment_search import AttachmentSearchQuery
    from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
    from officeflow.infrastructure.settings.store import AppSettings
    from officeflow.presentation.document_actions import DocumentBrowser
    from officeflow.presentation.main_window import MainWindow
    from tests.integration.test_document_lifecycle import lifecycle
    from tests.integration.test_reliability_phase2 import populate

    with lifecycle(tmp_path) as (env, facade):
        populate(env, facade.documents)
        exports = AttachmentExportService(
            env.attachments, ExternalExportStorage(tmp_path / "journals")
        )
        window = MainWindow(
            AppSettings(automatic_backup_enabled=False),
            env.tasks,
            record_service=env.records,
            attachment_service=env.attachments,
            attachment_search_service=env.search,
            attachment_export_service=exports,
        )
        qtbot.addWidget(window)
        hit = env.search.search_page(AttachmentSearchQuery(source_kind="version")).items[0]
        window._open_file_records(hit, True)
        box = next(box for box in window._file_dialogs if isinstance(box, DocumentBrowser))
        assert box.current().id == hit.attachment_id and box.current().kind == "version"
        assert box.list.count() <= 50
        window.shutdown()


def test_long_backup_caption_and_error_do_not_expand_small_window(qtbot, tmp_path):
    manager, _ = one_backup(tmp_path)
    dialog = BackupFilesDialog(manager)
    qtbot.addWidget(dialog)
    dialog.resize(500, 420)
    dialog.filename.setText("W" * 55)
    dialog._failed(ValueError("긴 오류 " * 1000))
    dialog.show()
    qtbot.wait(10)
    assert dialog.width() <= 520
    assert len(dialog.status.text()) < 240
    assert dialog.rect().contains(
        dialog.close_button.mapTo(dialog, dialog.close_button.rect().bottomRight())
    )
    dialog.reject()
