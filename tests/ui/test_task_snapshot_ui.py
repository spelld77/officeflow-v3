import gc
import weakref
from uuid import uuid4

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QThread
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QPushButton

from officeflow.application.task_snapshots import SnapshotCommand, SnapshotSelection
from officeflow.application.tasks import TaskDraft
from officeflow.presentation.task_history_dialog import TaskHistoryDialog
from officeflow.presentation.task_snapshot_dialog import SnapshotDetailDialog, TaskSnapshotDialog
from tests.integration.test_document_lifecycle import lifecycle, original


def settled(qtbot, dialog, condition=lambda: True):
    qtbot.waitUntil(lambda: dialog._thread is None and condition(), timeout=10000)


def test_capture_checks_across_pages_closed_confirmation_and_bounded_rows(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        task = env.tasks.create(TaskDraft("선택 검증"))
        for index in range(101):
            path = tmp_path / f"file-{index}.txt"
            path.write_text(str(index), encoding="utf-8")
            env.attachments.attach(task.id, path)
        dialog = TaskSnapshotDialog(task.id, facade.snapshots)
        qtbot.addWidget(dialog)
        dialog.show()
        settled(qtbot, dialog, lambda: dialog.list.count() == 100)
        dialog.list.item(0).setCheckState(Qt.CheckState.Checked)
        first = next(iter(dialog._selection))
        qtbot.mouseClick(dialog.next, Qt.MouseButton.LeftButton)
        settled(qtbot, dialog, lambda current=dialog: current.list.count() == 1)
        dialog.list.item(0).setCheckState(Qt.CheckState.Checked)
        assert len(dialog._selection) == 2 and first in dialog._selection
        assert not dialog.next.isEnabled()
        dialog.submit()
        assert "닫" in dialog.feedback.text() and dialog._thread is None
        dialog.closed.setChecked(True)
        dialog.submit()
        settled(qtbot, dialog, lambda: dialog.capture.text() == "보관 완료")
        assert env.tasks.history.page(task.id).items[0].file_count == 2
        assert dialog.list.count() == 1
        dialog.reject()


def test_snapshot_detail_info_stays_visible_and_trash_is_read_only(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        source = original(env, facade)
        env.records.add_checklist_item(source.task_id, "보관된 체크 내용")
        saved = facade.snapshots.create(
            SnapshotCommand(
                uuid4().hex,
                source.task_id,
                "보관",
                (SnapshotSelection("attachment", source.id),),
                documents_closed=True,
            )
        )
        entry = env.tasks.history.get(source.task_id, saved.snapshot_id, "snapshot")
        dialog = SnapshotDetailDialog(entry, facade, None, read_only=True)
        qtbot.addWidget(dialog)
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#202020"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#000000"))
        dialog.setPalette(palette)
        dialog.resize(450, 500)
        dialog.show()
        settled(qtbot, dialog, lambda: dialog.list.count() == 1)
        dialog.show_info()
        info = dialog._info_dialogs[0]
        settled(qtbot, info, lambda: bool(info.after.toPlainText()))
        assert "보관된 체크 내용" in info.after.toPlainText()
        assert source.name in info.after.toPlainText()
        assert not any(
            button.isEnabled()
            for button in dialog.findChildren(QPushButton)
            if button.text() in {"새 작업본…", "이 보관 삭제…"}
        )
        assert dialog.list.viewport().palette().color(QPalette.ColorRole.Base).lightness() > 200
        info.reject()
        assert not dialog._info_dialogs
        dialog.reject()


def test_one_hundred_history_open_close_cycles_release_workers_and_subscriptions(
    qtbot, task_service
):
    task = task_service.create(TaskDraft("반복 창"))
    references = []
    for _ in range(100):
        dialog = TaskHistoryDialog(task.id, task.title, task_service.history)
        references.append(weakref.ref(dialog))
        dialog.show()
        settled(qtbot, dialog, lambda current=dialog: current.list.count() == 1)
        dialog.list.setCurrentRow(0)
        dialog.open_detail()
        settled(qtbot, dialog, lambda current=dialog: bool(current._children))
        child = dialog._children[0]
        settled(qtbot, child)
        dialog.reject()
        assert not dialog.findChildren(QThread)
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        del child, dialog
    gc.collect()
    assert all(reference() is None for reference in references)


def test_capture_retry_after_commit_uses_same_identity_without_duplicate(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        task = env.tasks.create(TaskDraft("재시도 보관"))
        service = facade.snapshots
        create = service.create
        requests = []

        def ambiguous_success(command, **kwargs):
            requests.append(command.job_id)
            result = create(command, **kwargs)
            if len(requests) == 1:
                raise OSError("보관 확정 후 응답 중단")
            return result

        service.create = ambiguous_success
        dialog = TaskSnapshotDialog(task.id, service)
        qtbot.addWidget(dialog)
        dialog.show()
        settled(qtbot, dialog)
        dialog.submit()
        settled(qtbot, dialog, lambda: dialog.capture.isEnabled())
        assert "응답 중단" in dialog.feedback.text()
        dialog.submit()
        settled(qtbot, dialog, lambda: dialog.capture.text() == "보관 완료")
        assert len(requests) == 2 and requests[0] == requests[1]
        assert "다시 만들지" in dialog.feedback.text()
        saved = env.tasks.history.page(task.id)
        assert sum(item.kind == "snapshot" for item in saved.items) == 1
        dialog.reject()


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("screen", ["capture", "detail"])
def test_file_check_indicator_is_visible_and_clickable(qtbot, tmp_path, dark, screen):
    with lifecycle(tmp_path) as (env, facade):
        source = original(env, facade)
        if screen == "capture":
            dialog = TaskSnapshotDialog(source.task_id, facade.snapshots)
        else:
            saved = facade.snapshots.create(
                SnapshotCommand(
                    uuid4().hex,
                    source.task_id,
                    "표시 검증",
                    (SnapshotSelection("attachment", source.id),),
                    documents_closed=True,
                )
            )
            entry = env.tasks.history.get(source.task_id, saved.snapshot_id, "snapshot")
            dialog = SnapshotDetailDialog(entry, facade, None, read_only=False)
        qtbot.addWidget(dialog)
        if dark:
            palette = QPalette()
            for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base):
                palette.setColor(role, QColor("#202020"))
            palette.setColor(QPalette.ColorRole.Text, QColor("black"))
            dialog.setPalette(palette)
        dialog.resize(460, 520)
        dialog.show()
        settled(qtbot, dialog, lambda: dialog.list.count() == 1)
        item = dialog.list.item(0)
        row = dialog.list.visualItemRect(item)
        assert row.height() >= 32

        def blue_pixels():
            image = dialog.list.viewport().grab().toImage().scaled(dialog.list.viewport().size())
            return [
                (x, y)
                for x in range(row.left(), row.left() + 30)
                for y in range(row.top(), row.bottom() + 1)
                if (color := image.pixelColor(x, y)).blue() >= 140
                and color.blue() - color.red() >= 60
            ]

        unchecked = blue_pixels()
        assert len(unchecked) >= 24  # visible blue outline, not the pale system default
        center = QPoint(
            round(sum(x for x, _y in unchecked) / len(unchecked)),
            round(sum(y for _x, y in unchecked) / len(unchecked)),
        )
        qtbot.mouseClick(dialog.list.viewport(), Qt.MouseButton.LeftButton, pos=center)
        assert item.checkState() is Qt.CheckState.Checked
        assert len(blue_pixels()) > len(unchecked) + 50
        if screen == "capture":
            assert dialog.capture.text() == "선택 1개와 함께 보관"
            assert dialog.closed.isVisible()
        else:
            assert "체크 1개" in dialog.feedback.text()
        dialog.list.setCurrentItem(item)
        dialog.list.setFocus()
        qtbot.keyClick(dialog.list, Qt.Key.Key_Space)
        assert item.checkState() is Qt.CheckState.Unchecked
        if screen == "capture":
            assert dialog.capture.text() == "업무 정보만 보관"
        else:
            assert "체크 0개" in dialog.feedback.text()
        assert "왼쪽" in dialog.file_hint.text()
        dialog.reject()
