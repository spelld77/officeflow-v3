"""Isolated offscreen rendering; never opens a user profile or changes OS settings."""

import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_SCALE_FACTOR"] = sys.argv[2]

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QColor, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

from officeflow.application.reminder_preview import ReminderPreviewService
from officeflow.application.reminders import ReminderService
from officeflow.application.task_saving import ReminderEdit
from officeflow.application.tasks import TaskDraft
from officeflow.domain.enums import ReminderRelation
from officeflow.domain.reminder import ReminderRuleInput
from officeflow.infrastructure.database.reminder_repository import SqlAlchemyReminderRepository
from officeflow.infrastructure.database.task_saving import task_save_service
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow
from tests.integration.test_attachment_search import search_environment


def settle(test):
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: loop.quit() if test() else None)
    timer.start(10)
    QTimer.singleShot(15000, loop.quit)
    loop.exec()
    timer.stop()
    assert test()


output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
app = QApplication([])
for filename in ("malgun.ttf", "malgunbd.ttf", "segoeui.ttf"):
    assert QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / filename)) >= 0
if sys.argv[3] == "dark":
    palette = app.palette()
    for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
        palette.setColor(role, QColor("#202020"))
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(role, QColor("#000000"))
    app.setPalette(palette)
with (
    tempfile.TemporaryDirectory(prefix="officeflow-phase3-visual-") as temporary,
    search_environment(Path(temporary)) as env,
):
    now = datetime(2026, 10, 10, 1, tzinfo=UTC)
    repo = SqlAlchemyReminderRepository(env.sessions)
    service = ReminderService(repo, env.tasks)
    save = task_save_service(env.sessions, "Asia/Seoul")
    for number in range(4):
        task = save.save(
            TaskDraft(
                ("제출 준비 " if number == 0 else "업무 ")
                + str(number)
                + (" 긴 제목" * 20 if number == 0 else ""),
                starts_at=now + (timedelta(days=30) if number == 3 else timedelta()),
            ),
            edits=tuple(
                ReminderEdit(
                    ReminderRuleInput(
                        ReminderRelation.ABSOLUTE, absolute_at=now + timedelta(minutes=10 + i)
                    )
                )
                for i in range(3)
            ),
            now=now,
        )
        if number == 0:
            env.add(task.id, "첨부자료.txt")
    MainWindow._start_reminder_monitoring = lambda self: None
    window = MainWindow(
        AppSettings(automatic_backup_enabled=False),
        env.tasks,
        reminder_service=service,
        task_save_service=save,
        reminder_preview_service=ReminderPreviewService(repo, service),
        record_service=env.records,
        attachment_service=env.attachments,
        view_clock=lambda: now,
    )
    window.resize(760, 560)
    window.show()
    settle(lambda: len(window._today_reminders._result.items) == 12)
    preview = window._today_reminders
    preview.list.setCurrentRow(0)
    app.processEvents()
    assert window.width() == 760
    assert preview.list.count() == 2
    assert preview.rect().contains(
        preview.details_button.mapTo(preview, preview.details_button.rect().bottomRight())
    )
    assert window.grab().save(str(output / "today-groups.png"))
    preview.details_button.click()
    dialog = preview._details
    dialog.resize(460, 340)
    app.processEvents()
    assert dialog.list.count() == 3
    assert dialog.width() == 460
    for button in (dialog.records_button, dialog.settings_button, dialog.close_button):
        assert dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight()))
    assert (
        dialog.list.viewport()
        .grab()
        .toImage()
        .pixelColor(2, dialog.list.viewport().height() - 2)
        .lightness()
        > 180
    )
    assert dialog.grab().save(str(output / "alarm-details.png"))
    dialog.reject()
    window._show_calendar()
    page = window._calendar_page
    page._set_month_and_date(now.date())
    settle(lambda: page.day_list.count() == 4 and page.calendar._alarm_counts.get(now.date()) == 12)
    page.day_list.setCurrentRow(0)
    app.processEvents()
    assert window.width() == 760
    assert page.rect().contains(
        page.reminder_details_button.mapTo(page, page.reminder_details_button.rect().bottomRight())
    )
    assert (
        page.day_list.visualItemRect(page.day_list.item(2)).bottom()
        <= page.day_list.viewport().height()
    )
    assert (
        page.day_list.viewport()
        .grab()
        .toImage()
        .pixelColor(2, page.day_list.viewport().height() - 2)
        .lightness()
        > 180
    )
    assert window.grab().save(str(output / "calendar-groups.png"))
    assert page.day_panel.rect().contains(
        page.day_list.mapTo(page.day_panel, page.day_list.rect().bottomRight())
    ), f"panel={page.day_panel.geometry()}, list={page.day_list.geometry()}"
    assert page.day_list.geometry().bottom() < page.day_more_hint.geometry().top(), (
        f"list={page.day_list.geometry()}, hint={page.day_more_hint.geometry()}"
    )
    window.shutdown()
    window.close()
print("phase 3 visual assertions passed")
