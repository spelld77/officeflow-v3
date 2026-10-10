import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QDate, QPoint, QPointF, Qt, QTime
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette, QWheelEvent
from PySide6.QtWidgets import QApplication, QDialog

from officeflow.application.task_saving import ReminderEdit
from officeflow.domain.enums import ReminderRelation
from officeflow.domain.reminder import ReminderRuleInput as Rule
from officeflow.domain.task import Task
from officeflow.presentation.reminder_settings import ReminderRuleDialog
from officeflow.presentation.theme import LIGHT_STYLESHEET


def rule_dialog(qtbot, edit=None):
    task = Task.create(title="알림 입력", starts_at=datetime.now(UTC) + timedelta(days=30))
    widget = ReminderRuleDialog(task, edit=edit)
    widget.setStyleSheet(LIGHT_STYLESHEET)
    qtbot.addWidget(widget)
    if edit is None:
        widget.date_edit.setDate(QDate.currentDate().addDays(10))
    widget.show()
    widget.activateWindow()
    qtbot.wait(10)
    return widget


@pytest.mark.parametrize(
    "text,expected", [("11", QTime(11, 0)), ("1124", QTime(11, 24)), ("11:24", QTime(11, 24))]
)
def test_rule_whole_time_typing_and_save(qtbot, text, expected):
    widget = rule_dialog(qtbot)
    widget.time_edit.setFocus()
    assert widget.time_edit.selectedText() == widget.time_edit.text()
    qtbot.keyClicks(widget.time_edit, text)
    qtbot.keyClick(widget.time_edit, Qt.Key.Key_Tab)
    assert widget.time_edit.text() == expected.toString("HH:mm")
    widget._accept_rule()
    assert widget.result() == QDialog.DialogCode.Accepted
    result = widget.result_edit.rule.absolute_at.astimezone(ZoneInfo(widget._task.timezone))
    assert (result.hour, result.minute) == (expected.hour(), expected.minute())


def test_rule_paste_shift_tab_and_mouse_entry(qtbot, qapp):
    widget = rule_dialog(qtbot)
    clipboard = qapp.clipboard()
    original = clipboard.text()
    try:
        widget.date_edit.setFocus()
        assert widget.focusWidget() is widget.date_edit
        qtbot.mousePress(widget.time_edit, Qt.MouseButton.LeftButton)
        assert widget.time_edit.selectedText() == widget.time_edit.text()
        qtbot.mouseRelease(widget.time_edit, Qt.MouseButton.LeftButton)
        assert widget.time_edit.selectedText() == widget.time_edit.text()
        clipboard.setText("0924")
        qtbot.keyClick(widget.time_edit, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        qtbot.keyClick(widget.time_edit, Qt.Key.Key_Backtab)
        assert widget.time_edit.text() == "09:24"
        assert widget.focusWidget() is widget.date_edit
    finally:
        clipboard.setText(original)


def test_rule_invalid_time_blocks_tab_and_apply_but_relative_mode_works(qtbot):
    widget = rule_dialog(qtbot)
    widget.time_edit.setFocus()
    qtbot.keyClicks(widget.time_edit, "2460")
    qtbot.keyClick(widget.time_edit, Qt.Key.Key_Tab)
    assert widget.focusWidget() is widget.time_edit
    assert widget.time_edit.text() == "2460"
    assert widget.error.text()
    assert widget.time_edit.property("inputError")
    widget._accept_rule()
    assert widget.result_edit is None
    widget.mode.setCurrentIndex(widget.mode.findData("start"))
    assert not widget.time_input.isVisible()
    widget._accept_rule()
    assert widget.result() == QDialog.DialogCode.Accepted
    assert widget.result_edit.rule.relation is ReminderRelation.START


def test_rule_hour_adjustment_rolls_date_and_respects_bounds(qtbot):
    widget = rule_dialog(qtbot)
    day = widget.date_edit.date()
    widget.time_edit.setTime(QTime(23, 24))
    widget.time_input.plus_button.click()
    assert widget.date_edit.date() == day.addDays(1)
    assert widget.time_edit.time() == QTime(0, 24)
    widget.time_edit.setFocus()
    qtbot.keyClick(widget.time_edit, Qt.Key.Key_Down)
    assert widget.date_edit.date() == day
    assert widget.time_edit.time() == QTime(23, 24)
    qtbot.keyClicks(widget.time_edit, "11")
    qtbot.keyClick(widget.time_edit, Qt.Key.Key_Tab)
    assert widget.time_edit.time() == QTime(11, 0)
    widget.date_edit.setDate(widget.date_edit.maximumDate())
    widget.time_edit.setTime(QTime(23, 24))
    widget.time_input.plus_button.click()
    assert widget.time_edit.time() == QTime(23, 24)
    assert "범위" in widget.error.text()


@pytest.mark.parametrize(
    "field", ["date_edit", "time_edit", "mode", "offset", "unit", "direction", "quick"]
)
def test_rule_wheel_does_not_change_values(qtbot, field):
    widget = rule_dialog(qtbot)
    if field in {"offset", "unit", "direction", "quick"}:
        widget.mode.setCurrentIndex(widget.mode.findData("start"))
    target = getattr(widget, field)
    before = (
        widget.date_edit.date(),
        widget.time_edit.text(),
        widget.mode.currentIndex(),
        widget.offset.value(),
        widget.unit.currentIndex(),
        widget.direction.currentIndex(),
        widget.quick.currentIndex(),
    )
    wheel = QWheelEvent(
        QPointF(target.rect().center()),
        QPointF(target.mapToGlobal(target.rect().center())),
        QPoint(),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(target, wheel)
    after = (
        widget.date_edit.date(),
        widget.time_edit.text(),
        widget.mode.currentIndex(),
        widget.offset.value(),
        widget.unit.currentIndex(),
        widget.direction.currentIndex(),
        widget.quick.currentIndex(),
    )
    assert after == before


def test_existing_absolute_time_preserved_and_cancel_does_not_apply(qtbot):
    absolute = (datetime.now(UTC) + timedelta(days=10)).replace(
        hour=3, minute=24, second=0, microsecond=0
    )
    original = ReminderEdit(Rule(ReminderRelation.ABSOLUTE, absolute_at=absolute), 7)
    widget = rule_dialog(qtbot, original)
    local = absolute.astimezone(ZoneInfo(widget._task.timezone))
    assert widget.time_edit.time() == QTime(local.hour, local.minute)
    assert widget._edit() == original
    widget.time_edit.setText("1124")
    widget.reject()
    assert widget.result_edit is None
    assert original.rule.absolute_at == absolute


@pytest.mark.parametrize("dark", [False, True])
def test_rule_narrow_capture_and_readable_time(qtbot, qapp, dark):
    original_palette = QPalette(qapp.palette())
    original_font = QFont(qapp.font())
    widget = None
    try:
        if os.environ.get("OFFICEFLOW_RENDER_FONTS") == "1":
            assert QFontDatabase.addApplicationFont("C:/Windows/Fonts/malgun.ttf") >= 0
            qapp.setFont(QFont("Malgun Gothic", original_font.pointSize()))
        if dark:
            palette = QPalette(original_palette)
            for role in (
                QPalette.ColorRole.Window,
                QPalette.ColorRole.Base,
                QPalette.ColorRole.Button,
            ):
                palette.setColor(role, QColor("#202020"))
            for role in (
                QPalette.ColorRole.WindowText,
                QPalette.ColorRole.Text,
                QPalette.ColorRole.ButtonText,
            ):
                palette.setColor(role, QColor("#eeeeee"))
            qapp.setPalette(palette)
        widget = rule_dialog(qtbot)
        widget.resize(380, widget.sizeHint().height())
        qtbot.wait(10)
        assert widget.time_edit.palette().color(QPalette.ColorRole.Text).lightness() < 100
        assert widget.time_edit.palette().color(QPalette.ColorRole.Base).lightness() > 150
        for button in (widget.time_input.minus_button, widget.time_input.plus_button):
            assert widget.time_input.rect().contains(button.geometry())
            assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 8
        directory = Path("artifacts/reminder-time-input-captures")
        directory.mkdir(parents=True, exist_ok=True)
        suffix = f"{'dark' if dark else 'light'}-{os.environ.get('QT_SCALE_FACTOR', '1')}"
        assert widget.grab().save(str(directory / f"rule-{suffix}.png"))
    finally:
        if widget is not None:
            widget.reject()
        qapp.setPalette(original_palette)
        qapp.setFont(original_font)
