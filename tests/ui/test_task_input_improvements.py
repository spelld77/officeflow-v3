from datetime import UTC, date, datetime

import pytest
from PySide6.QtCore import QDate, QPoint, QPointF, Qt, QTime
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QDateTimeEdit, QDialog, QScrollArea

from officeflow.application.task_saving import ReminderEdit
from officeflow.domain.enums import ReminderRelation
from officeflow.domain.reminder import ReminderRuleInput
from officeflow.domain.task import Task
from officeflow.presentation.task_editor import TaskEditorDialog


def editor(qtbot):
    widget = TaskEditorDialog(timezone="Asia/Seoul", initial_date=date(2026, 10, 10))
    qtbot.addWidget(widget)
    widget.show()
    widget.activateWindow()
    qtbot.waitUntil(widget.isActiveWindow)
    widget.title_edit.setText("입력 검증")
    return widget


@pytest.mark.parametrize(
    "control",
    [
        "start_date_edit",
        "start_time_edit",
        "priority_combo",
        "status_combo",
        "schedule_combo",
        "repeat_combo",
        "repeat_interval",
    ],
)
def test_wheel_does_not_change_editor_values(qtbot, control):
    widget = editor(qtbot)
    widget.advanced_schedule_check.setChecked(True)
    field = getattr(widget, control)
    field.setFocus()
    if control == "start_date_edit":
        field.setCurrentSection(QDateTimeEdit.Section.YearSection)
    before = widget._build_draft(), widget.repeat_interval.value()
    event = QWheelEvent(
        QPointF(12, 12),
        QPointF(field.mapToGlobal(QPoint(12, 12))),
        QPoint(),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(field, event)
    assert (widget._build_draft(), widget.repeat_interval.value()) == before


@pytest.mark.parametrize(
    "text,expected", [("11", QTime(11, 0)), ("1124", QTime(11, 24)), ("11:24", QTime(11, 24))]
)
def test_whole_time_typing_replaces_value_and_tab_moves(qtbot, text, expected):
    widget = editor(qtbot)
    widget.title_edit.setFocus()
    widget.start_time_edit.setTime(QTime(9, 30))
    widget.start_time_edit.setFocus()
    assert widget.start_time_edit.selectedText() == "09:30"
    qtbot.keyClicks(widget.start_time_edit, text)
    qtbot.keyClick(widget.start_time_edit, Qt.Key.Key_Tab)
    assert widget.start_time_edit.time() == expected
    assert widget.start_time_edit.text() == expected.toString("HH:mm")
    assert widget.focusWidget() is widget.reminder_settings_button


def test_invalid_time_is_retained_and_save_focuses_error(qtbot):
    widget = editor(qtbot)
    widget.start_time_edit.setText("2460")
    widget._validate_and_accept()
    assert widget.isVisible()
    assert widget.start_time_edit.text() == "2460"
    assert widget.error_label.isVisible()
    assert widget.focusWidget() is widget.start_time_edit


def test_hour_buttons_and_arrows_preserve_minutes_and_change_date(qtbot):
    widget = editor(qtbot)
    widget.start_date_edit.setDate(QDate(2026, 12, 31))
    widget.start_time_edit.setTime(QTime(23, 24))
    qtbot.mouseClick(widget.start_time_input.plus_button, Qt.MouseButton.LeftButton)
    assert widget.start_date_edit.date() == QDate(2027, 1, 1)
    assert widget.start_time_edit.time() == QTime(0, 24)
    widget.start_time_edit.setFocus()
    qtbot.keyClick(widget.start_time_edit, Qt.Key.Key_Down)
    assert widget.start_date_edit.date() == QDate(2026, 12, 31)
    assert widget.start_time_edit.time() == QTime(23, 24)


def test_no_schedule_suspends_only_untouched_default_alarm_and_restores(qtbot):
    widget = editor(qtbot)
    original = widget.reminder_edits()
    widget.schedule_combo.setCurrentIndex(widget.schedule_combo.findData("none"))
    assert widget.reminder_edits() == ()
    widget.schedule_combo.setCurrentIndex(widget.schedule_combo.findData("day"))
    assert widget.reminder_edits() == original
    widget.schedule_combo.setCurrentIndex(widget.schedule_combo.findData("none"))
    widget._validate_and_accept()
    assert widget.draft().starts_at is None
    assert widget.draft().reminder_rules == ()


def test_custom_and_existing_alarms_are_not_silently_removed(qtbot):
    task = Task.create(title="기존", starts_at=datetime(2026, 10, 10, 1, tzinfo=UTC))
    widget = TaskEditorDialog(
        timezone="Asia/Seoul",
        task=task,
        reminder_rules=(ReminderRuleInput(ReminderRelation.START, -10),),
    )
    qtbot.addWidget(widget)
    widget.show()
    original = widget.reminder_edits()
    widget.schedule_combo.setCurrentIndex(widget.schedule_combo.findData("none"))
    assert widget.reminder_edits() == original
    widget._validate_and_accept()
    assert widget.isVisible()
    assert widget.focusWidget() is widget.reminder_settings_button


def test_end_default_tracks_start_but_direct_end_is_preserved(qtbot):
    widget = editor(qtbot)
    widget.start_time_edit.setTime(QTime(18, 30))
    widget.advanced_schedule_check.setChecked(True)
    widget.end_enabled_check.setChecked(True)
    assert widget.end_time_edit.time() == QTime(19, 30)
    widget.end_time_edit.setTime(QTime(20, 15))
    widget.start_time_edit.setTime(QTime(21, 0))
    assert widget.end_time_edit.time() == QTime(20, 15)
    widget._validate_and_accept()
    assert widget.isVisible()
    assert widget.focusWidget() is widget.end_time_edit


def test_hidden_invalid_end_does_not_block_start_only_save(qtbot):
    widget = editor(qtbot)
    widget.end_time_edit.setText("bad")
    widget._validate_and_accept()
    assert widget.draft().ends_at is None


def test_actual_paste_and_shift_tab_commit_whole_value(qtbot):
    widget = editor(qtbot)
    field = widget.start_time_edit
    field.setFocus()
    clipboard = QApplication.clipboard()
    original = clipboard.text()
    try:
        clipboard.setText("1124")
        qtbot.keyClick(field, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        qtbot.keyClick(field, Qt.Key.Key_Backtab)
        assert field.time() == QTime(11, 24)
        assert field.text() == "11:24"
        assert widget.focusWidget() is widget.start_date_edit
    finally:
        clipboard.setText(original)


def test_wheel_scrolls_body_and_open_combo_is_usable(qtbot):
    widget = editor(qtbot)
    widget.advanced_schedule_check.setChecked(True)
    widget.resize(460, 520)
    QApplication.processEvents()
    scroll = widget.findChild(QScrollArea)
    bar = scroll.verticalScrollBar()
    bar.setValue(0)
    field = widget.start_time_edit
    previous = field.time()
    QApplication.sendEvent(
        field,
        QWheelEvent(
            QPointF(5, 5),
            QPointF(field.mapToGlobal(QPoint(5, 5))),
            QPoint(),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        ),
    )
    assert field.time() == previous
    assert bar.value() > 0
    widget.priority_combo.showPopup()
    assert widget.priority_combo.view().isVisible()
    widget.priority_combo.setCurrentIndex(2)
    widget.priority_combo.hidePopup()
    assert widget.priority_combo.currentText() == "중요"


@pytest.mark.parametrize(
    "now,expected",
    [
        (datetime(2026, 10, 10, 14, 10), datetime(2026, 10, 10, 16)),
        (datetime(2026, 12, 31, 23, 30), datetime(2027, 1, 1, 1)),
    ],
)
def test_editor_default_and_calendar_date_use_stable_clock(qtbot, monkeypatch, now, expected):
    from zoneinfo import ZoneInfo

    import officeflow.presentation.task_editor as module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.replace(tzinfo=ZoneInfo("Asia/Seoul")).astimezone(tz)

    monkeypatch.setattr(module, "datetime", Clock)
    widget = TaskEditorDialog(timezone="Asia/Seoul")
    qtbot.addWidget(widget)
    assert widget.start_date_edit.date().toPython() == expected.date()
    assert widget.start_time_edit.time() == QTime(expected.hour, 0)
    selected = TaskEditorDialog(timezone="Asia/Seoul", initial_date=date(2026, 11, 15))
    qtbot.addWidget(selected)
    assert selected.start_date_edit.date() == QDate(2026, 11, 15)
    assert selected.start_time_edit.time() == QTime(expected.hour, 0)


def test_existing_end_and_custom_new_alarm_are_preserved(qtbot):
    task = Task.create(
        title="기존 종료",
        starts_at=datetime(2026, 10, 10, 1, tzinfo=UTC),
        ends_at=datetime(2026, 10, 10, 3, tzinfo=UTC),
    )
    widget = TaskEditorDialog(timezone="Asia/Seoul", task=task)
    qtbot.addWidget(widget)
    widget.start_date_edit.setDate(QDate(2026, 10, 11))
    assert widget.end_date_edit.date() == QDate(2026, 10, 10)
    assert widget.end_time_edit.time() == QTime(12, 0)
    new = editor(qtbot)
    custom = (ReminderEdit(ReminderRuleInput(ReminderRelation.START, -10)),)
    new._reminder_edits = custom
    new.schedule_combo.setCurrentIndex(new.schedule_combo.findData("none"))
    assert new.reminder_edits() == custom
    new._validate_and_accept()
    assert new.isVisible()


def test_hour_wrap_end_requires_range_without_changing_value(qtbot):
    widget = editor(qtbot)
    widget.advanced_schedule_check.setChecked(True)
    widget.end_enabled_check.setChecked(True)
    widget.end_time_edit.setTime(QTime(23, 24))
    qtbot.mouseClick(widget.end_time_input.plus_button, Qt.MouseButton.LeftButton)
    assert widget.end_time_edit.time() == QTime(23, 24)
    assert "기간 일정" in widget.error_label.text()
    widget.schedule_combo.setCurrentIndex(widget.schedule_combo.findData("range"))
    previous = widget.end_date_edit.date()
    qtbot.mouseClick(widget.end_time_input.plus_button, Qt.MouseButton.LeftButton)
    assert widget.end_date_edit.date() == previous.addDays(1)
    assert widget.end_time_edit.time() == QTime(0, 24)


def test_range_auto_end_rolls_over_and_title_and_repeat_errors_focus(qtbot):
    widget = editor(qtbot)
    widget.schedule_combo.setCurrentIndex(widget.schedule_combo.findData("range"))
    widget.start_time_edit.setTime(QTime(23, 30))
    assert widget.end_date_edit.date() == widget.start_date_edit.date().addDays(1)
    assert widget.end_time_edit.time() == QTime(0, 30)
    widget.title_edit.clear()
    widget._validate_and_accept()
    assert widget.focusWidget() is widget.title_edit
    widget.title_edit.setText("정상 제목")
    widget.repeat_combo.setCurrentIndex(widget.repeat_combo.findData("DAILY"))
    widget.repeat_until_check.setChecked(True)
    widget.repeat_until_date.setDate(widget.start_date_edit.date().addDays(-1))
    widget._validate_and_accept()
    assert widget.focusWidget() is widget.repeat_until_date
    assert widget.result() != QDialog.DialogCode.Accepted


def test_hour_step_leaves_time_ready_for_replacement_and_invalid_can_cancel(qtbot):
    widget = editor(qtbot)
    widget.start_time_edit.setFocus()
    qtbot.keyClick(widget.start_time_edit, Qt.Key.Key_Up)
    qtbot.keyClicks(widget.start_time_edit, "11")
    qtbot.keyClick(widget.start_time_edit, Qt.Key.Key_Tab)
    assert widget.start_time_edit.time() == QTime(11, 0)
    widget.start_time_edit.setText("bad")
    widget.start_time_edit.setFocus()
    qtbot.mouseClick(widget.cancel_button, Qt.MouseButton.LeftButton)
    assert not widget.isVisible()


def test_date_bounds_do_not_wrap_time_on_wrong_day(qtbot):
    widget = editor(qtbot)
    widget.start_date_edit.setDate(widget.start_date_edit.minimumDate())
    widget.start_time_edit.setTime(QTime(0, 24))
    previous = widget.start_date_edit.date()
    qtbot.mouseClick(widget.start_time_input.minus_button, Qt.MouseButton.LeftButton)
    assert widget.start_date_edit.date() == previous
    assert widget.start_time_edit.time() == QTime(0, 24)
    assert "범위" in widget.error_label.text()


def test_error_reopens_hidden_end_settings_without_overwriting_input(qtbot):
    widget = editor(qtbot)
    widget.advanced_schedule_check.setChecked(True)
    widget.end_enabled_check.setChecked(True)
    widget.end_time_edit.setFocus()
    widget.end_time_edit.selectAll()
    qtbot.keyClicks(widget.end_time_edit, "bad")
    widget.advanced_schedule_check.setChecked(False)
    widget._validate_and_accept()
    assert widget.advanced_schedule_check.isChecked()
    assert widget.end_time_edit.isVisible()
    assert widget.end_time_edit.text() == "bad"
    assert widget.focusWidget() is widget.end_time_edit
