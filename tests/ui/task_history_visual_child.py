"""Render only isolated Phase-3 fixtures; no operating profile, editor or desktop clicks."""

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_SCALE_FACTOR"] = sys.argv[2]

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtGui import QColor, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QComboBox, QDateEdit, QLineEdit, QPushButton, QWidget

from officeflow.application.task_snapshots import SnapshotCommand, SnapshotSelection
from officeflow.application.tasks import TaskDraft
from officeflow.presentation.task_history_dialog import HistoryCompareDialog, TaskHistoryDialog
from officeflow.presentation.task_snapshot_dialog import (
    PendingSnapshotDialog,
    SnapshotDetailDialog,
    TaskSnapshotDialog,
)
from tests.integration.test_document_lifecycle import lifecycle, original


def settled(dialog):
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: loop.quit() if dialog._thread is None else None)
    timer.start(10)
    QTimer.singleShot(15_000, loop.quit)
    loop.exec()
    timer.stop()
    assert dialog._thread is None, "worker did not finish"


root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
app = QApplication([])
for font in ("malgun.ttf", "malgunbd.ttf", "segoeui.ttf", "segoeuib.ttf"):
    font_id = QFontDatabase.addApplicationFont(
        str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / font)
    )
    assert font_id >= 0, "Windows font could not be loaded for visual verification"
palette = app.palette()
dark = sys.argv[3] == "dark"
if dark:
    for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
        palette.setColor(role, QColor("#202020"))
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(role, QColor("#000000"))
app.setPalette(palette)
with lifecycle(root / "profile") as (env, facade):
    source = original(env, facade)
    task = env.tasks.get(source.task_id)
    env.records.add_checklist_item(task.id, "팀장 검토 전 체크 항목 · 긴 내용도 스크롤로 읽습니다")
    env.tasks.update(task.id, TaskDraft(task.title, description="예산 100만원\n첫 설명"))
    first = facade.snapshots.create(
        SnapshotCommand(
            uuid4().hex,
            task.id,
            "팀장 검토 전",
            (SnapshotSelection("attachment", source.id),),
            documents_closed=True,
        )
    )
    env.tasks.update(task.id, TaskDraft(task.title, description="예산 100(천원)\n검토 후 설명"))
    entry = env.tasks.history.get(task.id, first.snapshot_id, "snapshot")
    dialogs = (
        ("history", TaskHistoryDialog(task.id, task.title, env.tasks.history, lifecycle=facade)),
        ("capture", TaskSnapshotDialog(task.id, facade.snapshots)),
        ("detail", SnapshotDetailDialog(entry, facade, None, read_only=False)),
        (
            "compare",
            HistoryCompareDialog(
                "두 시점 비교",
                entry.after,
                env.tasks.history.current(task.id),
                timezone=task.timezone,
                left_info=entry.saved_info,
            ),
        ),
        ("pending", PendingSnapshotDialog(task.id, facade.snapshots)),
    )
    results = []
    for name, dialog in dialogs:
        dialog.resize(480, 520)
        dialog.show()
        settled(dialog)
        if name == "history":
            dialog.date_filter.setChecked(True)
            settled(dialog)
        assert dialog.width() <= 520, f"{name} forced excessively wide: {dialog.width()}"
        for widget in dialog.findChildren(QWidget):
            if (
                isinstance(widget, (QPushButton, QComboBox, QLineEdit, QDateEdit))
                and widget.isVisible()
            ):
                assert dialog.rect().contains(widget.mapTo(dialog, widget.rect().topLeft()))
                assert dialog.rect().contains(widget.mapTo(dialog, widget.rect().bottomRight()))
        png = root / (name + ".png")
        assert dialog.grab().save(str(png))
        if name in {"capture", "detail"} and dialog.list.count():
            dialog.list.item(0).setCheckState(Qt.CheckState.Checked)
            app.processEvents()
            for widget in dialog.findChildren(QWidget):
                if isinstance(widget, QPushButton) and widget.isVisible():
                    assert dialog.rect().contains(widget.mapTo(dialog, widget.rect().bottomRight()))
            assert dialog.grab().save(str(root / (name + "-checked.png")))
            dialog.list.setEnabled(False)
            assert dialog.grab().save(str(root / (name + "-disabled.png")))
            dialog.list.setEnabled(True)
        results.append(
            {
                "screen": name,
                "width": dialog.width(),
                "height": dialog.height(),
                "dpr": dialog.devicePixelRatioF(),
                "path": str(png),
            }
        )
    for _, dialog in dialogs:
        dialog.reject()
        dialog.deleteLater()
print(json.dumps(results))
