# ruff: noqa: F811
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import Qt

from officeflow.infrastructure.attachments.drag_copies import DragCopyStore
from tests.ui.test_attachment_export_ui import dialog, files  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "officeflow.presentation.attachment_drag.DragCopyStore",
        lambda service: DragCopyStore(service, tmp_path / "drag"),
    )


@pytest.mark.parametrize("view", ["list", "thumbnails"])
def test_selected_files_offer_copy_only_urls_and_preserve_internal_files(
    dialog, qtbot, monkeypatch, view
):
    import officeflow.presentation.attachment_drag as module

    dialog._set_attachment_view(view)
    listing = dialog.attachment_list
    listing.setCurrentRow(0)
    listing.item(2).setSelected(True)
    controller = dialog._drag_controller
    qtbot.waitUntil(lambda: controller._ready is not None, timeout=5000)
    batch = controller._ready
    seen = []

    def execute(drag, actions, default):
        seen.append((actions, default, [Path(url.toLocalFile()) for url in drag.mimeData().urls()]))
        assert listing.property("outgoingAttachmentDrag")
        return Qt.DropAction.CopyAction

    monkeypatch.setattr(module.QDrag, "exec", execute)
    listing.startDrag(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction)
    assert seen == [(Qt.DropAction.CopyAction, Qt.DropAction.CopyAction, list(batch.paths))]
    assert batch.offered and len(batch.paths) == 2
    assert all(p.exists() for p in batch.paths)
    assert not listing.property("outgoingAttachmentDrag")
    assert len(dialog._attachments_by_id) == 4
    assert listing.dragEnabled()


def test_preparing_drag_never_starts_native_drag(dialog, monkeypatch):
    listing = dialog.attachment_list
    listing.setCurrentRow(0)
    monkeypatch.setattr(
        "officeflow.presentation.attachment_drag.QDrag.exec",
        lambda *args: pytest.fail("drag started before preparation"),
    )
    listing.startDrag(Qt.DropAction.CopyAction)
    assert "다시 끌어" in dialog.attachment_drag_status.text()


def test_selection_change_cancels_old_and_coalesces_latest(dialog, qtbot, monkeypatch):
    controller = dialog._drag_controller
    original = controller.store.prepare
    entered = Event()

    def prepare(refs, canceled):
        entered.set()
        while not canceled():
            if released.wait(0.01):
                break
        return original(refs, canceled)

    released = Event()
    monkeypatch.setattr(controller.store, "prepare", prepare)
    dialog.attachment_list.setCurrentRow(0)
    qtbot.waitUntil(entered.is_set, timeout=3000)
    dialog.attachment_list.setCurrentRow(2)
    released.set()
    qtbot.waitUntil(lambda: controller._ready is not None, timeout=5000)
    assert controller._token == (dialog._attachments_by_id[dialog._selected_attachment_ids()[0]],)
    assert len(controller._ready.paths) == 1


def test_clear_selection_invalidates_ready_and_does_not_drag(dialog, qtbot, monkeypatch):
    listing = dialog.attachment_list
    controller = dialog._drag_controller
    listing.setCurrentRow(0)
    qtbot.waitUntil(lambda: controller._ready is not None, timeout=5000)
    listing.clearSelection()
    controller.invalidate()
    assert controller._ready is None
    monkeypatch.setattr(
        "officeflow.presentation.attachment_drag.QDrag.exec",
        lambda *args: pytest.fail("empty selection dragged"),
    )
    controller.start_drag()


def test_shutdown_during_preparation_joins_worker(dialog, qtbot):
    controller = dialog._drag_controller
    dialog.attachment_list.setCurrentRow(0)
    controller._timer.stop()
    controller._start()
    controller.request_shutdown()
    qtbot.waitUntil(lambda: controller._shutdown_scope.is_stopped, timeout=5000)
    assert controller._ready is None
    assert controller._thread is None


def test_mouse_threshold_routes_to_outgoing_drag(dialog, qtbot, monkeypatch):
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    listing = dialog.attachment_list
    listing.setCurrentRow(0)
    controller = dialog._drag_controller
    qtbot.waitUntil(lambda: controller._ready is not None, timeout=5000)
    seen = []
    monkeypatch.setattr(
        "officeflow.presentation.attachment_drag.QDrag.exec", lambda *args: seen.append(True)
    )
    position = listing.visualItemRect(listing.item(0)).center()
    qtbot.mousePress(listing.viewport(), Qt.MouseButton.LeftButton, pos=position)
    for delta in (20, 40):
        point = QPointF(position.x() + delta, position.y())
        event = QMouseEvent(
            QEvent.Type.MouseMove,
            point,
            QPointF(listing.viewport().mapToGlobal(point.toPoint())),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(listing.viewport(), event)
    qtbot.mouseRelease(listing.viewport(), Qt.MouseButton.LeftButton, pos=position)
    assert seen


def test_drag_from_this_app_is_not_reimported(qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QMimeData, QPoint, QUrl
    from PySide6.QtGui import QDragEnterEvent
    from PySide6.QtWidgets import QWidget

    from officeflow.presentation.attachment_drop import AttachmentDropRouter

    source, target = QWidget(), QWidget()
    qtbot.addWidget(source)
    qtbot.addWidget(target)
    source.setProperty("outgoingAttachmentDrag", True)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tmp_path / "staged.txt"))])
    event = QDragEnterEvent(
        QPoint(5, 5),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    monkeypatch.setattr(QDragEnterEvent, "source", lambda _event: source)
    received = []
    router = AttachmentDropRouter(target, (target,), lambda: True, received.append)
    assert router.handle(target, event)
    assert not event.isAccepted()
    assert not received and not router.pending


@pytest.mark.parametrize("dark,view", [(False, "list"), (True, "thumbnails")])
def test_drag_status_narrow_render(dialog, qtbot, qapp, dark, view):
    from PySide6.QtGui import QColor, QPalette

    previous = qapp.palette()
    palette = QPalette(previous)
    if dark:
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#202020"))
        for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text):
            palette.setColor(role, QColor("#eeeeee"))
    qapp.setPalette(palette)
    try:
        dialog.resize(500, 540)
        dialog._set_attachment_view(view)
        dialog.attachment_list.setCurrentRow(0)
        dialog.attachment_list.item(2).setSelected(True)
        qtbot.waitUntil(lambda: dialog._drag_controller._ready is not None, timeout=5000)
        qtbot.wait(20)
        status = dialog.attachment_drag_status
        assert status.isVisible() and "2개" in status.text()
        assert status.height() >= status.heightForWidth(status.width())
        directory = Path("artifacts/u41-drag/captures")
        directory.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(directory / f"{view}-dark-{dark}.png"))
    finally:
        qapp.setPalette(previous)
