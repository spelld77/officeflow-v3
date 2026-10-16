from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QDialog, QDialogButtonBox
from pytestqt.qtbot import QtBot

from officeflow.application.tasks import TaskDraft, TaskService, TaskView
from officeflow.domain.enums import OccurrenceStatus, TaskStatus
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow
from officeflow.presentation.record_dialog import CompleteTaskDialog
from officeflow.presentation.theme import LIGHT_STYLESHEET


@pytest.mark.parametrize("selection", ["cleared", "other-task", "other-occurrence"])
@pytest.mark.parametrize("entry", ["list", "calendar"])
def test_completion_input_keeps_original_target_during_modal_events(
    qtbot: QtBot, task_service: TaskService, selection: str, entry: str
) -> None:
    start = datetime.now(UTC).replace(microsecond=0)
    recurring = selection == "other-occurrence"
    task = task_service.create(
        TaskDraft(
            title="완료 내용을 입력할 업무",
            starts_at=start,
            recurrence_rule="FREQ=DAILY;INTERVAL=1" if recurring else None,
        )
    )
    other = task_service.create(TaskDraft(title="변경되면 안 되는 다른 업무"))
    assert task.id is not None and other.id is not None
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window._set_view(TaskView.ALL)
    if entry == "calendar":
        window._show_calendar()
    window._select_task_for_action(task)
    opened = []

    def enter_and_save() -> None:
        dialog = window.findChild(CompleteTaskDialog)
        assert dialog is not None
        try:
            opened.append(dialog)
            dialog.result_edit.setPlainText("검수하고 제출 완료")
            # A pending search refresh runs inside QDialog.exec()'s event loop.
            window._search.setText("변경되면 안 되는")
            window._search_timer.stop()
            window._refresh_tasks()
            assert window._selected_task_id is None
            if selection == "other-task":
                window._select_task_for_action(other)
            elif recurring:
                window._selected_task_id = task.id
                window._selected_occurrence_start = start + timedelta(days=1)
            buttons = dialog.findChild(QDialogButtonBox)
            assert buttons is not None
            buttons.button(QDialogButtonBox.StandardButton.Save).click()
        finally:
            if dialog.isVisible():
                dialog.reject()

    timer = QTimer(window)
    timer.setSingleShot(True)
    timer.timeout.connect(enter_and_save)
    timer.start(0)
    window._complete_selected_with_result()

    assert len(opened) == 1
    assert task_service.result_note(task.id, start if recurring else None) == "검수하고 제출 완료"
    assert task_service.get(other.id).status is TaskStatus.ACTIVE
    assert task_service.result_note(other.id) == ""
    if recurring:
        occurrence = task_service._repository.get_occurrence(task.id, start)
        assert occurrence is not None and occurrence.status is OccurrenceStatus.COMPLETED
        next_occurrence = task_service._repository.get_occurrence(
            task.id, start + timedelta(days=1)
        )
        assert next_occurrence is None or next_occurrence.status is OccurrenceStatus.ACTIVE
        assert task_service.get(task.id).status is TaskStatus.ACTIVE
    else:
        assert task_service.get(task.id).status is TaskStatus.COMPLETED


def test_completion_input_retains_text_until_save_succeeds(qtbot: QtBot) -> None:
    from officeflow.domain.task import Task

    task = Task.create(title="저장 재시도", now=datetime.now(UTC))
    calls: list[str] = []

    def save(note: str) -> None:
        calls.append(note)
        if len(calls) == 1:
            raise OSError("임시 저장 실패")

    dialog = CompleteTaskDialog(task, on_save=save)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.result_edit.setPlainText("잃어버리면 안 되는 내용")
    buttons = dialog.findChild(QDialogButtonBox)
    assert buttons is not None
    buttons.button(QDialogButtonBox.StandardButton.Save).click()
    assert dialog.isVisible()
    assert dialog.result_note() == "잃어버리면 안 되는 내용"
    assert "임시 저장 실패" in dialog.error_label.text()
    buttons.button(QDialogButtonBox.StandardButton.Save).click()
    assert not dialog.isVisible()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert calls == ["잃어버리면 안 되는 내용"] * 2


def test_completion_input_database_failure_rolls_back_and_retries(
    qtbot: QtBot, task_service: TaskService, monkeypatch
) -> None:
    from officeflow.infrastructure.database.task_history_repository import HistoryWriter

    task = task_service.create(TaskDraft(title="DB 실패 후 완료 재시도"))
    assert task.id is not None
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window._set_view(TaskView.ALL)
    window._select_task_for_action(task)
    append = HistoryWriter.append
    command_ids = []
    complete = task_service.complete

    def track_complete(*args, **kwargs):
        command_ids.append(kwargs["command_id"])
        return complete(*args, **kwargs)

    def fail(*args, **kwargs):
        raise OSError("이력 DB 쓰기 실패")

    monkeypatch.setattr(task_service, "complete", track_complete)
    monkeypatch.setattr(HistoryWriter, "append", fail)

    def enter_and_save() -> None:
        dialog = window.findChild(CompleteTaskDialog)
        assert dialog is not None
        try:
            dialog.result_edit.setPlainText("재시도할 완료 내용")
            buttons = dialog.findChild(QDialogButtonBox)
            assert buttons is not None
            save = buttons.button(QDialogButtonBox.StandardButton.Save)
            save.click()
            assert dialog.isVisible()
            assert "이력 DB 쓰기 실패" in dialog.error_label.text()
            assert dialog.result_note() == "재시도할 완료 내용"
            assert task_service.get(task.id).status is TaskStatus.ACTIVE
            assert task_service.result_note(task.id) == ""
            assert len(task_service.history.page(task.id).items) == 1
            monkeypatch.setattr(HistoryWriter, "append", append)
            save.click()
            assert not dialog.isVisible()
        finally:
            if dialog.isVisible():
                dialog.reject()

    timer = QTimer(window)
    timer.setSingleShot(True)
    timer.timeout.connect(enter_and_save)
    timer.start(0)
    window._complete_selected_with_result()
    assert task_service.get(task.id).status is TaskStatus.COMPLETED
    assert task_service.result_note(task.id) == "재시도할 완료 내용"
    assert len(command_ids) == 2 and command_ids[0] == command_ids[1]
    history = task_service.history.page(task.id)
    assert len(history.items) == 2
    detail = task_service.history.get(task.id, history.items[0].id)
    assert set(detail.changed_fields) == {"status", "result_note"}


@pytest.mark.parametrize("save", [True, False])
def test_completion_input_empty_save_and_cancel(
    qtbot: QtBot, task_service: TaskService, save: bool
) -> None:
    task = task_service.create(TaskDraft(title="완료 또는 취소"))
    assert task.id is not None
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window._set_view(TaskView.ALL)
    window._select_task_for_action(task)

    def finish() -> None:
        dialog = window.findChild(CompleteTaskDialog)
        assert dialog is not None
        if save:
            dialog.accept()
        else:
            dialog.result_edit.setPlainText("취소할 내용")
            dialog.reject()

    timer = QTimer(window)
    timer.setSingleShot(True)
    timer.timeout.connect(finish)
    timer.start(0)
    window._complete_selected_with_result()
    assert task_service.result_note(task.id) == ""
    assert task_service.get(task.id).status is (TaskStatus.COMPLETED if save else TaskStatus.ACTIVE)


@pytest.mark.parametrize("dark", [False, True])
def test_completion_input_error_is_readable_and_buttons_fit(qapp, qtbot: QtBot, dark: bool) -> None:
    from officeflow.domain.task import Task

    original = QPalette(qapp.palette())
    if dark:
        palette = QPalette(original)
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#202020"))
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
        ):
            palette.setColor(role, QColor("#eeeeee"))
        qapp.setPalette(palette)

    def save(_note: str) -> None:
        raise OSError("저장소가 일시적으로 사용 중입니다. 잠시 후 다시 시도하세요.")

    dialog = CompleteTaskDialog(Task.create(title="완료 내용 입력"), on_save=save)
    qtbot.addWidget(dialog)
    try:
        dialog.setStyleSheet(LIGHT_STYLESHEET)
        dialog.show()
        dialog.result_edit.setPlainText("검수 후 전달했습니다. 저장 실패 시 이 내용은 유지됩니다.")
        dialog.accept()
        qtbot.wait(30)
        assert dialog.error_label.isVisible()
        assert dialog.palette().color(QPalette.ColorRole.Window).name() == "#f4f6fa"
        assert dialog.error_label.palette().color(QPalette.ColorRole.WindowText).name() == "#c62828"
        assert dialog.result_edit.palette().color(QPalette.ColorRole.Text).name() == "#172033"
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons is not None
        for role in (QDialogButtonBox.StandardButton.Save, QDialogButtonBox.StandardButton.Cancel):
            button = buttons.button(role)
            assert button.isVisible() and button.isEnabled()
            assert dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight()))
        image = Path("artifacts") / f"completion-input-error-{'dark' if dark else 'light'}.png"
        assert dialog.grab().save(str(image))
    finally:
        dialog.reject()
        qapp.setPalette(original)
