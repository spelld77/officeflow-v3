from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import Qt

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.attachment_export import AttachmentExportService
from officeflow.infrastructure.attachments.drag_copies import DragCopyStore
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
from officeflow.infrastructure.database.document_catalog import DocumentPage
from officeflow.presentation.document_actions import DocumentBrowser
from tests.integration.test_document_lifecycle import lifecycle
from tests.integration.test_multiple_attachment_delete import pair


@pytest.fixture
def browser(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "officeflow.presentation.attachment_drag.DragCopyStore",
        lambda service: DragCopyStore(service, tmp_path / "drag-copies"),
    )
    with lifecycle(tmp_path / "profile") as (env, facade):
        first, second = pair(env)
        source = tmp_path / "third.txt"
        source.write_bytes(b"third")
        third = env.attachments.attach(first.task_id, source)
        for index, item in enumerate((first, second, third)):
            facade.create(
                facade.catalog.get("attachment", item.id),
                DocumentOperation.VERSION,
                label=f"보관 {index}",
            )
        exports = AttachmentExportService(
            env.attachments, ExternalExportStorage(tmp_path / "journal")
        )
        box = DocumentBrowser(facade, exports, first.task_id)
        qtbot.addWidget(box)
        box.show()
        yield box, env, facade
        box.shutdown()


@pytest.mark.parametrize("kind", ["version", "attachment"])
def test_drag_uses_highlights_not_checks_and_preserves_sources(browser, qtbot, monkeypatch, kind):
    box, env, _ = browser
    box.kind.setCurrentIndex(0 if kind == "version" else 1)
    box.list.item(2).setCheckState(Qt.CheckState.Checked)
    checked = set(box.selected)
    box.list.setCurrentRow(0)
    box.list.item(1).setSelected(True)
    controller = box._drag_controller
    qtbot.waitUntil(lambda: controller._ready is not None, timeout=5000)
    refs = box._drag_references()
    assert len(refs) == 2 and all(ref.kind.value == kind for ref in refs)
    source_bytes = [box.exporter._service.resolve(ref).path.read_bytes() for ref in refs]
    seen = []

    def execute(drag, actions, default):
        paths = [Path(url.toLocalFile()) for url in drag.mimeData().urls()]
        seen.extend(paths)
        assert actions == default == Qt.DropAction.CopyAction
        assert [path.read_bytes() for path in paths] == source_bytes
        assert all(not path.is_relative_to(env.attachments.storage_root) for path in paths)

    monkeypatch.setattr("officeflow.presentation.attachment_drag.QDrag.exec", execute)
    box.list.startDrag(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction)
    assert len(seen) == 2 and box.selected == checked
    assert [box.exporter._service.resolve(ref).path.read_bytes() for ref in refs] == source_bytes
    assert not controller.status.isVisible()


def test_page_change_invalidates_ready_and_does_not_drag_hidden_checks(browser, qtbot, monkeypatch):
    box, _, facade = browser
    rows = tuple(box.entries.values())
    monkeypatch.setattr(
        facade.catalog,
        "page_with_more",
        lambda *args, before=None, **kwargs: (
            DocumentPage(rows[:2], True) if before is None else DocumentPage(rows[2:], False)
        ),
    )
    box.refresh()
    box.list.item(0).setCheckState(Qt.CheckState.Checked)
    checked = set(box.selected)
    box.list.setCurrentRow(0)
    qtbot.waitUntil(lambda: box._drag_controller._ready is not None, timeout=5000)
    box.next()
    assert box._drag_controller._ready is None
    assert box._drag_references() == () and box.selected == checked


def test_kind_and_refresh_invalidate_prepared_snapshot(browser, qtbot):
    box, _, _ = browser
    box.list.setCurrentRow(0)
    qtbot.waitUntil(lambda: box._drag_controller._ready is not None, timeout=5000)
    box.kind.setCurrentIndex(1)
    assert box._drag_controller._ready is None
    box.list.setCurrentRow(0)
    qtbot.waitUntil(lambda: box._drag_controller._ready is not None, timeout=5000)
    box.refresh()
    assert box._drag_controller._ready is None


def test_close_cancels_preparation_without_late_drag(browser, qtbot, monkeypatch):
    box, _, _ = browser
    entered = Event()
    original = box._drag_controller.store.prepare

    def prepare(refs, canceled):
        entered.set()
        while not canceled():
            Event().wait(0.01)
        return original(refs, canceled)

    monkeypatch.setattr(box._drag_controller.store, "prepare", prepare)
    box.list.setCurrentRow(0)
    qtbot.waitUntil(entered.is_set, timeout=5000)
    box.shutdown()
    assert box._drag_controller._closed
    assert box._drag_controller._thread is None


@pytest.mark.parametrize("dark", [False, True])
def test_browser_drag_narrow_render_has_no_extra_status_row(browser, qtbot, qapp, dark):
    from PySide6.QtGui import QColor, QPalette

    box, _, _ = browser
    previous = qapp.palette()
    try:
        if dark:
            palette = QPalette(previous)
            for role in (
                QPalette.ColorRole.Window,
                QPalette.ColorRole.Base,
                QPalette.ColorRole.Button,
            ):
                palette.setColor(role, QColor("#202020"))
            qapp.setPalette(palette)
        box.resize(480, 380)
        box.list.setCurrentRow(0)
        qtbot.waitUntil(lambda: box._drag_controller._ready is not None, timeout=5000)
        assert not box._drag_controller.status.isVisible()
        assert box.save_button.isVisible() and box.list.isVisible()
        output = Path("artifacts/u44-browser-drag/captures")
        output.mkdir(parents=True, exist_ok=True)
        assert box.grab().save(str(output / f"browser-dark-{dark}.png"))
    finally:
        qapp.setPalette(previous)
