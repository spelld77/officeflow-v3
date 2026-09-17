from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from officeflow.application.reminders import ReminderAlert
from officeflow.domain.enums import (
    ReminderDeliveryStatus,
    ReminderRelation,
    TaskPriority,
    TaskStatus,
)
from officeflow.domain.reminder import Reminder, ReminderDelivery
from officeflow.domain.task import Task
from officeflow.presentation.reminder_dialog import ReminderDialog


def test_reminder_dialog_stays_on_top_and_emits_custom_snooze(qtbot: QtBot) -> None:
    now = datetime(2026, 9, 14, 3, 0, tzinfo=UTC)
    task = Task.create(
        title="주간 보고",
        description="보고서를 제출합니다.",
        priority=TaskPriority.IMPORTANT,
        status=TaskStatus.ACTIVE,
        starts_at=now,
        now=now,
    )
    task = Task(
        id=7,
        title=task.title,
        description=task.description,
        status=task.status,
        priority=task.priority,
        is_pinned=task.is_pinned,
        all_day=task.all_day,
        starts_at=task.starts_at,
        ends_at=task.ends_at,
        timezone=task.timezone,
        recurrence_rule=task.recurrence_rule,
        result_note=task.result_note,
        completed_at=task.completed_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )
    reminder = Reminder(
        id=3,
        task_id=7,
        relation=ReminderRelation.START,
        offset_minutes=0,
        absolute_at=None,
        enabled=True,
    )
    delivery = ReminderDelivery(
        id=11,
        reminder_id=3,
        task_id=7,
        occurrence_start=now,
        scheduled_at=now,
        fire_key="task:7|occurrence:now|reminder:3|at:now",
        status=ReminderDeliveryStatus.FIRED,
        first_fired_at=now,
        last_fired_at=now,
        snoozed_until=None,
        acknowledged_at=None,
        created_at=now,
        updated_at=now,
    )
    dialog = ReminderDialog(
        (ReminderAlert(delivery, reminder, task, recovered=True),),
        timezone="Asia/Seoul",
    )
    qtbot.addWidget(dialog)
    dialog.show()

    assert dialog.alert_list.count() == 1
    assert "놓친 알림" in dialog.caption.text()
    assert dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert dialog.snooze_minutes.value() == 10
    dialog.snooze_minutes.setValue(45)
    with qtbot.waitSignal(dialog.snoozeRequested) as blocker:
        qtbot.mouseClick(dialog.snooze_button, Qt.MouseButton.LeftButton)

    assert blocker.args == [11, 45]

    dialog.close()
    assert dialog.isVisible()
