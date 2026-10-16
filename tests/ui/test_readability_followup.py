from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from PySide6.QtCore import QDate, QRect, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QLabel, QListWidget, QStyle, QStyleOptionButton, QVBoxLayout, QWidget

from officeflow.application.day_overview import DayOverviewResult
from officeflow.application.input_review import DateInput, review_dates
from officeflow.application.reminder_preview import ReminderPreviewItem
from officeflow.application.tasks import ScheduledTask
from officeflow.domain.enums import OccurrenceStatus, TaskPriority, TaskStatus
from officeflow.domain.task import Task
from officeflow.presentation.day_calendar import DayCalendarPanel, value_text
from officeflow.presentation.reminder_settings import ReminderRuleDialog
from officeflow.presentation.task_editor import TaskEditorDialog
from officeflow.presentation.theme import LIGHT_STYLESHEET

NOW = datetime(2026, 10, 16, 9, tzinfo=UTC)


@pytest.fixture
def editor(qtbot):
    editor = TaskEditorDialog(timezone="UTC", now=NOW, attachment_service=object())
    editor.setStyleSheet(LIGHT_STYLESHEET)
    qtbot.addWidget(editor)
    editor.show()
    qtbot.wait(60)
    yield editor
    editor.reject()


def test_normal_date_change_has_no_dedicated_notice_space(editor):
    editor.start_date_edit.setDate(QDate(2026, 10, 20))
    assert editor.start_date_caution.notice.isHidden()
    assert not editor.start_date_edit.property("dateCaution")
    assert editor.findChild(QLabel, "inputDateNotice") is None


def test_risky_year_highlights_field_and_clears_on_reset(editor):
    original = editor.start_date_edit.date()
    editor.start_date_edit.setDate(original.addYears(1))
    assert editor.start_date_edit.property("dateCaution")
    assert "2026 → 2027" in editor.start_date_caution.notice.text()
    assert not editor.start_date_caution.notice.isHidden()
    assert "2027" in editor.start_date_caution.notice.toolTip()
    editor.start_date_edit.setDate(original)
    assert not editor.start_date_edit.property("dateCaution")
    assert editor.start_date_caution.notice.isHidden()


def test_validation_error_color_takes_precedence_over_date_caution(editor, qtbot):
    from officeflow.presentation.input_controls import mark_input_error

    field = editor.start_date_edit
    field.setDate(field.date().addYears(1))
    field.setFocus()
    mark_input_error(field, True)
    qtbot.wait(30)
    assert field.palette().color(QPalette.ColorRole.Base).name() == "#fff4f4"
    mark_input_error(field, False)
    assert field.palette().color(QPalette.ColorRole.Base).name() == "#fff4dd"


def test_short_year_boundary_is_not_highlighted(qtbot):
    task = Task.create(title="연말", starts_at=datetime(2026, 12, 31, tzinfo=UTC))
    editor = TaskEditorDialog(timezone="UTC", task=task, now=NOW)
    qtbot.addWidget(editor)
    editor.start_date_edit.setDate(QDate(2027, 1, 1))
    assert not editor.start_date_edit.property("dateCaution")
    assert editor.start_date_caution.notice.isHidden()


def test_large_same_year_shift_is_reviewed():
    values = (DateInput("시작일", date(2026, 1, 1), date(2026, 8, 1)),)
    assert any(item.confirm for item in review_dates(values, today=NOW.date()))


def test_range_end_caution_clears_when_schedule_is_disabled(editor):
    editor.schedule_combo.setCurrentIndex(editor.schedule_combo.findData("range"))
    editor.end_date_edit.setDate(editor.end_date_edit.date().addYears(1))
    assert editor.end_date_edit.property("dateCaution")
    assert not editor.end_date_caution.notice.isHidden()
    editor.schedule_combo.setCurrentIndex(editor.schedule_combo.findData("none"))
    assert not editor.end_date_edit.property("dateCaution")
    assert editor.end_date_caution.notice.isHidden()


def test_collapsed_repeat_keeps_date_caution_signpost(editor):
    editor.advanced_schedule_check.setChecked(True)
    editor.repeat_combo.setCurrentIndex(editor.repeat_combo.findData("DAILY"))
    editor.repeat_until_check.setChecked(True)
    editor.repeat_until_date.setDate(editor.repeat_until_date.date().addYears(1))
    assert editor.repeat_until_date.property("dateCaution")
    editor.advanced_schedule_check.setChecked(False)
    assert "날짜 확인" in editor.advanced_schedule_check.text()


def test_existing_far_task_title_edit_does_not_warn(qtbot):
    task = Task.create(title="장기", starts_at=datetime(2029, 1, 1, tzinfo=UTC))
    editor = TaskEditorDialog(timezone="UTC", task=task, now=NOW)
    qtbot.addWidget(editor)
    editor.title_edit.setText("장기 제목 변경")
    assert not editor.start_date_edit.property("dateCaution")
    assert not editor._date_notices()


def test_explicit_initial_far_date_is_highlighted_without_forced_confirmation(qtbot):
    editor = TaskEditorDialog(timezone="UTC", initial_date=date(2029, 1, 1), now=NOW)
    qtbot.addWidget(editor)
    assert editor.start_date_edit.property("dateCaution")
    assert not any(notice.confirm for notice in editor._date_notices())


def test_direct_alarm_maintenance_hint_stays_in_alarm_row(editor):
    from officeflow.application.task_saving import ReminderEdit
    from officeflow.domain.enums import ReminderRelation
    from officeflow.domain.reminder import ReminderRuleInput

    editor._reminder_edits = (
        ReminderEdit(ReminderRuleInput(ReminderRelation.ABSOLUTE, absolute_at=NOW)),
    )
    editor.start_date_edit.setDate(QDate(2026, 10, 20))
    assert "유지" in editor.reminder_notice.full_text
    assert (
        editor.reminder_notice.parentWidget()
        is editor.reminder_summary.parentWidget().parentWidget()
    )
    assert not editor.start_date_edit.property("dateCaution")


def test_alarm_rule_date_uses_same_caution(qtbot):
    child = ReminderRuleDialog(Task.create(title="알림", timezone="UTC"))
    qtbot.addWidget(child)
    original = child.date_edit.date()
    child.date_edit.setDate(original.addYears(1))
    assert child.date_edit.property("dateCaution")
    assert not child.date_row.notice.isHidden()
    child.date_edit.setDate(original)
    assert not child.date_edit.property("dateCaution")


def test_buttons_fit_narrow_editor_and_share_footer_height(editor, qtbot):
    editor.resize(460, 600)
    qtbot.wait(60)
    assert editor.save_button.height() == editor.cancel_button.height() >= 40
    assert min(editor.save_button.width(), editor.cancel_button.width()) >= 72
    point = editor.cancel_button.mapTo(editor, editor.cancel_button.rect().bottomRight())
    assert editor.rect().right() - point.x() >= 20
    for button in (editor.start_time_input.minus_button, editor.start_time_input.plus_button):
        option = QStyleOptionButton()
        button.initStyleOption(option)
        content = button.style().subElementRect(
            QStyle.SubElement.SE_PushButtonContents, option, button
        )
        assert content.width() >= button.fontMetrics().horizontalAdvance(button.text())
        assert button.width() >= button.sizeHint().width()
    assert editor._scroll.horizontalScrollBar().maximum() == 0


@pytest.mark.parametrize("status", list(TaskStatus))
def test_day_schedule_detail_has_each_status(status):
    from officeflow.presentation.calendar_style import day_status

    task = Task.create(title="업무", starts_at=NOW)
    value = ScheduledTask(replace(task, status=status), NOW, None)
    assert day_status(value) in value_text(value, "UTC")
    if status is TaskStatus.ARCHIVED:
        assert "보관" in value_text(value, "UTC") and "취소" not in value_text(value, "UTC")


@pytest.mark.parametrize("status", ["예정", "재알림", "미확인", "시각 지남 · 발생 기록 없음"])
def test_day_alarm_badge_is_not_task_status(status):
    from officeflow.presentation.calendar_style import day_status

    task = Task.create(title="업무", starts_at=NOW)
    value = ReminderPreviewItem(task, 1, None, NOW, NOW, status)
    assert day_status(value) == ("확인 필요" if "기록 없음" in status else status)


@pytest.mark.parametrize("priority", list(TaskPriority))
def test_month_and_day_share_priority_color(priority):
    from officeflow.presentation.calendar_style import calendar_color
    from officeflow.presentation.month_calendar import MonthCalendarWidget

    task = Task.create(title="업무", starts_at=NOW, priority=priority)
    value = ScheduledTask(task, NOW, None)
    assert MonthCalendarWidget._task_color(value).name() == calendar_color(value)


def test_recurrence_state_and_all_day_list_keep_completion(qtbot):
    from officeflow.presentation.calendar_style import day_status

    task = Task.create(title="종일", starts_at=NOW, all_day=True)
    value = ScheduledTask(task, NOW, None, NOW, OccurrenceStatus.COMPLETED)
    panel = DayCalendarPanel("UTC", None)
    qtbot.addWidget(panel)
    panel.set_day(NOW.date())
    panel.set_result(DayOverviewResult(NOW.date(), (value,), True))
    assert day_status(value) == "완료"
    assert "완료" in panel.all_day.item(0).text()
    assert not panel.all_day.item(0).icon().isNull()
    panel.set_mode("schedule-list")
    assert "완료" in panel.entries.list.item(0).text()
    panel.shutdown()


def test_mixed_group_is_not_painted_as_one_completed_task(qtbot):
    panel = DayCalendarPanel("UTC", None)
    panel.setStyleSheet(LIGHT_STYLESHEET)
    qtbot.addWidget(panel)
    values = tuple(
        ScheduledTask(
            replace(
                Task.create(title=f"업무 {i}", starts_at=NOW),
                id=i,
                status=TaskStatus.COMPLETED if i < 3 else TaskStatus.ACTIVE,
            ),
            NOW,
            None,
        )
        for i in range(6)
    )
    panel.resize(560, 460)
    panel.show()
    panel.set_day(NOW.date())
    panel.set_result(DayOverviewResult(NOW.date(), values, True))
    qtbot.wait(40)
    mark = next(mark for mark in panel.timeline.marks if len(mark.values) > 1)
    assert mark.values[0].status is TaskStatus.COMPLETED
    rect = panel.timeline.mark_rect(mark)
    image = panel.timeline.grab(QRect(int(rect.left()) + 8, int(rect.top()) + 26, 4, 4)).toImage()
    assert image.pixelColor(1, 1).name() == "#eaf1ff"
    panel._show_group(mark.values)
    listing = panel._dialogs[-1].findChild(QListWidget)
    texts = [listing.item(i).text() for i in range(listing.count())]
    assert any("완료" in text for text in texts) and any("진행" in text for text in texts)
    panel.shutdown()


@pytest.mark.parametrize("dark", [False, True])
def test_compact_editor_and_popup_visual(editor, qtbot, qapp, dark):
    original = QPalette(qapp.palette())
    palette = QPalette(original)
    if dark:
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#202020"))
    qapp.setPalette(palette)
    try:
        editor.resize(460, 600)
        editor.start_date_edit.setFocus()
        qtbot.wait(30)
        editor.start_date_edit.setDate(QDate(2027, 10, 16))
        qtbot.wait(80)
        caution = editor.start_date_caution
        assert caution.notice.height() <= caution.notice.fontMetrics().lineSpacing() + 8
        assert caution.notice.y() <= caution.edit.height() + 4
        assert caution.edit.palette().color(QPalette.ColorRole.Base).name() == "#fff4dd"
        viewport = editor._scroll.viewport()
        assert viewport.rect().contains(
            caution.notice.mapTo(viewport, caution.notice.rect().bottomRight())
        )
        for button in (editor.save_button, editor.cancel_button):
            assert editor.rect().contains(button.mapTo(editor, button.rect().bottomRight()))
        scale = editor.devicePixelRatioF()
        assert editor.grab().save(f"artifacts/readability-editor-{dark}-{scale}.png")
        editor._open_reminder_settings()
        settings = editor._reminder_settings
        settings._open_rule(False)
        child = settings._child
        child.date_edit.setDate(child.date_edit.date().addYears(1))
        qtbot.wait(80)
        assert child.grab().save(f"artifacts/readability-alarm-{dark}-{scale}.png")
        settings.reject()
    finally:
        qapp.setPalette(original)


@pytest.mark.parametrize("dark", [False, True])
def test_day_status_stripes_and_short_cards_visual(qtbot, qapp, dark):
    from officeflow.application.reminder_preview import ReminderPreviewResult
    from officeflow.presentation.calendar_style import calendar_color

    original = QPalette(qapp.palette())
    palette = QPalette(original)
    if dark:
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#202020"))
    qapp.setPalette(palette)
    owner = QWidget()
    owner.setObjectName("appRoot")
    owner.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
    owner.setStyleSheet(LIGHT_STYLESHEET)
    qtbot.addWidget(owner)
    layout = QVBoxLayout(owner)
    panel = DayCalendarPanel("UTC", owner)
    panel.timeline.clock = lambda: NOW
    layout.addWidget(panel)
    try:
        values = []
        for number, status in enumerate(TaskStatus):
            start = NOW.replace(hour=8 + number)
            priority = list(TaskPriority)[number % 4]
            task = replace(
                Task.create(title="제목이 긴 업무 상태 확인", starts_at=start, priority=priority),
                status=status,
                id=number + 1,
            )
            values.append(ScheduledTask(task, start, None))
        for hour, priority in ((13, TaskPriority.IMPORTANT), (14, TaskPriority.URGENT)):
            start = NOW.replace(hour=hour)
            end = start.replace(hour=14) if hour == 13 else None
            task = replace(
                Task.create(
                    title="중요도 색과 기간 확인", starts_at=start, ends_at=end, priority=priority
                ),
                id=hour,
            )
            values.append(ScheduledTask(task, start, end))
        all_day = replace(values[2], task=replace(values[2].task, all_day=True))
        alarms = tuple(
            ReminderPreviewItem(
                values[0].task,
                i + 1,
                None,
                NOW.replace(hour=8 + i),
                NOW.replace(hour=8 + i),
                status,
            )
            for i, status in enumerate(("예정", "재알림", "미확인", "시각 지남 · 발생 기록 없음"))
        )
        owner.resize(560, 620)
        owner.show()
        panel.set_day(NOW.date())
        panel.set_result(DayOverviewResult(NOW.date(), (*values, all_day), True))
        panel.set_reminders(ReminderPreviewResult(alarms, True))
        qtbot.wait(100)
        canvas = panel.timeline
        for value in values:
            mark = next(mark for mark in canvas.marks if mark.values == (value,))
            rect = canvas.mark_rect(mark)
            stripe = canvas.grab(QRect(int(rect.left()), int(rect.top()), 10, 22)).toImage()
            dpr = stripe.devicePixelRatio()
            assert stripe.pixelColor(round(4 * dpr), round(10 * dpr)).name() == calendar_color(
                value
            )
        assert owner.grab().save(
            f"artifacts/readability-day-{dark}-{owner.devicePixelRatioF()}.png"
        )
    finally:
        panel.shutdown()
        owner.close()
        qapp.setPalette(original)
