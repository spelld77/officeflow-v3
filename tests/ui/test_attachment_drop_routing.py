from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QWidget

from officeflow.presentation.attachment_drop import AttachmentDropRouter


def send_drop(widget, sources, actions=Qt.DropAction.CopyAction, point=None):
    point = point or QPoint(8, 8)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(source)) for source in sources])
    enter = QDragEnterEvent(
        point, actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    )
    QApplication.sendEvent(widget, enter)
    move = QDragMoveEvent(
        point, actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    )
    QApplication.sendEvent(widget, move)
    drop = QDropEvent(
        QPointF(point), actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    )
    QApplication.sendEvent(widget, drop)
    return enter, move, drop


def test_drop_is_copy_only_queued_and_not_duplicated(qtbot, tmp_path):
    surface = QWidget()
    qtbot.addWidget(surface)
    surface.show()
    received = []
    router = AttachmentDropRouter(surface, (surface,), lambda: True, received.append)
    source = tmp_path / "source.txt"
    source.write_text("untouched")
    events = send_drop(surface, (source,), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction)
    assert all(event.isAccepted() for event in events)
    assert events[-1].dropAction() == Qt.DropAction.CopyAction
    assert received == []
    assert router.pending
    assert not send_drop(surface, (source,))[-1].isAccepted()
    qtbot.waitUntil(lambda: len(received) == 1)
    assert received == [(source,)]
    assert source.read_text() == "untouched"
    assert not router.pending


@pytest.mark.parametrize(
    "allowed,actions", [(False, Qt.DropAction.CopyAction), (True, Qt.DropAction.MoveAction)]
)
def test_unavailable_or_move_only_is_rejected(qtbot, tmp_path, allowed, actions):
    surface = QWidget()
    qtbot.addWidget(surface)
    received = []
    router = AttachmentDropRouter(surface, (surface,), lambda: allowed, received.append)
    assert not send_drop(surface, (tmp_path / "candidate.txt",), actions)[0].isAccepted()
    assert not received
    assert not router.pending


def test_cancelled_owner_does_not_deliver_queued_drop(qtbot, tmp_path):
    surface = QWidget()
    qtbot.addWidget(surface)
    received = []
    router = AttachmentDropRouter(surface, (surface,), lambda: True, received.append)
    assert send_drop(surface, (tmp_path / "candidate.txt",))[-1].isAccepted()
    router.shutdown()
    QApplication.processEvents()
    assert received == []


@pytest.mark.parametrize(
    "receiver",
    ["attachment_empty_label", "attachment_stack", "attachment_scroll", "attachment_list", "body"],
)
@pytest.mark.parametrize("seed,view", [(0, "list"), (1, "list"), (1, "thumbnails")])
def test_record_dialog_surface_drop_imports_once(
    qtbot, tmp_path, receiver, monkeypatch, seed, view
):
    from PySide6.QtWidgets import QMessageBox

    from officeflow.application.attachments import AttachmentService
    from officeflow.application.tasks import TaskDraft
    from officeflow.infrastructure.attachments.storage import ManagedAttachmentStorage
    from officeflow.presentation.record_dialog import TaskRecordsDialog
    from tests.ui.test_record_dialog import make_dialog_services
    from tests.unit.test_attachment_service import InMemoryAttachmentRepository

    tasks, records = make_dialog_services()
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    task = tasks.create(TaskDraft(title="Drop routing"))
    service = AttachmentService(
        InMemoryAttachmentRepository(), ManagedAttachmentStorage(tmp_path / "managed"), tasks
    )
    source = tmp_path / "source.txt"
    source.write_text("copy me")
    if seed:
        previous = tmp_path / "existing.txt"
        previous.write_text("old file")
        service.attach(task.id, previous)
    dialog = TaskRecordsDialog(
        task,
        task_service=tasks,
        record_service=records,
        attachment_service=service,
        initial_tab="attachments",
    )
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._set_attachment_view(view)
    if seed and receiver == "attachment_empty_label":
        dialog.attachment_kind_combo.setCurrentIndex(dialog.attachment_kind_combo.findData("work"))
    target = dialog.attachment_scroll.widget() if receiver == "body" else getattr(dialog, receiver)
    if receiver in {"attachment_scroll", "attachment_list"}:
        target = target.viewport()
    point = QPoint(target.width() - 2, target.height() - 2) if receiver == "body" else QPoint(8, 8)
    assert send_drop(target, (source,), point=point)[-1].isAccepted()
    qtbot.waitUntil(
        lambda: not dialog._attachment_drop_router.pending and dialog._attachment_thread is None,
        timeout=5000,
    )
    assert not warnings
    assert dialog.attachment_list.count() == 1 + seed
    assert len(service.attachments_for_task(task.id)) == 1 + seed
    assert source.read_text() == "copy me"


def test_nonlocal_urls_and_button_drop_are_rejected(qtbot):
    from PySide6.QtWidgets import QPushButton

    surface = QWidget()
    qtbot.addWidget(surface)
    surface.resize(160, 100)
    button = QPushButton("Control", surface)
    button.setGeometry(0, 0, 100, 35)
    surface.show()
    received = []
    router = AttachmentDropRouter(surface, (surface,), lambda: True, received.append)
    assert not send_drop(surface, (Path("fake.txt"),))[0].isAccepted()
    mime = QMimeData()
    mime.setUrls([QUrl("https://example.test/file.txt")])
    event = QDragEnterEvent(
        QPoint(120, 60),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(surface, event)
    assert not event.isAccepted()
    assert not router.pending and not received


def test_drop_after_switching_tab_is_not_added(qtbot, tmp_path):
    surface = QWidget()
    qtbot.addWidget(surface)
    available = [True]
    received = []
    router = AttachmentDropRouter(surface, (surface,), lambda: available[0], received.append)
    assert send_drop(surface, (tmp_path / "candidate.txt",))[-1].isAccepted()
    available[0] = False
    QApplication.processEvents()
    assert not router.pending and received == []
