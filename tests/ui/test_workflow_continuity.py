# ruff: noqa: F811
from datetime import timedelta

import pytest
from PySide6.QtCore import QDate, QModelIndex
from PySide6.QtWidgets import QDialogButtonBox, QMessageBox

from officeflow.application.tasks import TaskDraft, TaskView
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow
from officeflow.presentation.record_dialog import TaskRecordsDialog
from officeflow.presentation.schedule_change_dialog import ScheduleChangeDialog
from tests.integration.test_multiple_reminders import NOW, environment  # noqa: F401
from tests.ui.test_record_dialog import make_dialog_services


@pytest.fixture
def workflow_window(qtbot, environment, monkeypatch):
    _, tasks, _, reminders, save = environment
    monkeypatch.setattr(MainWindow, "_start_reminder_monitoring", lambda self: None)
    window = MainWindow(
        AppSettings(),
        tasks,
        reminder_service=reminders,
        task_save_service=save,
        view_clock=lambda: NOW,
    )
    qtbot.addWidget(window)
    window.resize(760, 560)
    window.show()
    yield window, tasks
    window.shutdown()


def test_tab_returns_selection_scroll_and_loaded_page(workflow_window, qtbot):
    window, tasks = workflow_window
    for i in range(90):
        tasks.create(TaskDraft(f"업무 {i:03}"), now=NOW + timedelta(seconds=i))
    window._set_view(TaskView.ALL)
    window._task_model.fetchMore(QModelIndex())
    index = window._task_model.index(70, 0)
    task = window._task_model.task_at(index)
    window._task_list.setCurrentIndex(index)
    window._task_list.scrollTo(index)
    qtbot.wait(25)
    scroll = window._task_list.verticalScrollBar().value()
    window._set_view(TaskView.UPCOMING)
    window._set_view(TaskView.ALL)
    qtbot.wait(25)
    assert window._selected_task_id == task.id
    assert window._task_model.loaded_task_count == 90
    assert abs(window._task_list.verticalScrollBar().value() - scroll) <= 2


def test_search_not_restored_and_quick_add_wins(workflow_window, qtbot):
    window, tasks = workflow_window
    tasks.create(TaskDraft("기존 업무"), now=NOW)
    window._set_view(TaskView.ALL)
    window._search.setText("기존")
    window._search_timer.stop()
    window._refresh_tasks()
    window._set_view(TaskView.UPCOMING)
    window._set_view(TaskView.ALL)
    assert not window._search.text()
    window._quick_add_edit.setText("새 업무")
    window._quick_add_task()
    new_id = window._selected_task_id
    qtbot.wait(25)
    assert tasks.get(new_id).title == "새 업무" and window._selected_task_id == new_id


def test_result_save_is_inline_and_edit_marks_unsaved(qtbot, monkeypatch):
    tasks, records = make_dialog_services()
    task = tasks.create(TaskDraft("요약"))
    dialog = TaskRecordsDialog(task, task_service=tasks, record_service=records)
    qtbot.addWidget(dialog)
    monkeypatch.setattr(
        QMessageBox, "information", lambda *_a: pytest.fail("성공 모달은 없어야 함")
    )
    dialog.result_edit.setPlainText("저장할 요약")
    assert "미저장" in dialog.result_save_feedback.full_text
    dialog._save_result()
    assert tasks.result_note(task.id) == "저장할 요약"
    assert dialog.result_save_feedback.full_text == "저장됨"
    dialog.result_edit.setPlainText("추가 수정")
    assert "미저장" in dialog.result_save_feedback.full_text


def test_move_keeps_target_and_undo_restores_unscheduled(workflow_window, qtbot):
    window, tasks = workflow_window
    task = tasks.create(TaskDraft("일정 없는 업무"), now=NOW)
    other = tasks.create(TaskDraft("다른 업무"), now=NOW)
    window._set_view(TaskView.ALL)
    window._open_schedule_change(task, tomorrow=True)
    dialog = window.findChild(ScheduleChangeDialog)
    assert dialog and not dialog.isModal()
    window._select_task_for_action(other)
    dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()
    qtbot.wait(25)
    assert tasks.get(task.id).starts_at is not None
    assert tasks.get(other.id).starts_at is None
    assert window._current_view is TaskView.ALL and window._schedule_result_bar.isVisible()
    window._undo_schedule_move()
    assert tasks.get(task.id).starts_at is None


def test_move_error_preserves_draft_and_changed_task_blocks_undo(workflow_window):
    window, tasks = workflow_window
    task = tasks.create(TaskDraft("충돌"), now=NOW)
    window._open_schedule_change(task, tomorrow=True)
    dialog = window.findChild(ScheduleChangeDialog)
    dialog.date_edit.setDate(QDate(2026, 10, 8))
    tasks.update(task.id, TaskDraft("다른 창 변경"), now=NOW + timedelta(seconds=1))
    dialog._submit()
    assert dialog.isVisible() and "변경" in dialog.error.text()
    assert dialog.date_edit.date() == QDate(2026, 10, 8)
    assert tasks.get(task.id).starts_at is None


def test_context_move_actions_available_but_not_for_completed(workflow_window):
    window, tasks = workflow_window
    task = tasks.create(TaskDraft("업무"), now=NOW)
    actions = [a.text() for a in window._build_task_context_menu(task).actions()]
    assert "내일로 이동…" in actions and "날짜·시간 변경…" in actions


def test_move_receipt_expires_without_changing_saved_date(workflow_window, qtbot):
    window, tasks = workflow_window
    task = tasks.create(TaskDraft("짧은 이동 안내"), now=NOW)
    window._open_schedule_change(task, tomorrow=True)
    window.findChild(ScheduleChangeDialog)._submit()
    saved_start = tasks.get(task.id).starts_at
    timer = window._schedule_result_timer
    assert timer.isSingleShot() and timer.isActive() and timer.interval() == 8000
    qtbot.wait(20)
    content_height = window._task_content.height()
    timer.setInterval(30)
    qtbot.waitUntil(
        lambda: (
            not window._schedule_result_bar.isVisible()
            and window._task_content.height() > content_height
        ),
        timeout=1000,
    )
    assert window._schedule_undo is None and window._schedule_result_day is None
    assert tasks.get(task.id).starts_at == saved_start
    assert window.grab().save(f"artifacts/move-notice-dismissed-{window.devicePixelRatioF()}.png")


@pytest.mark.parametrize("action", ["tab", "calendar", "close", "undo", "shutdown"])
def test_move_receipt_dismisses_when_finished(workflow_window, action):
    from PySide6.QtWidgets import QPushButton

    window, tasks = workflow_window
    task = tasks.create(TaskDraft("안내 닫힘"), now=NOW)
    window._open_schedule_change(task, tomorrow=True)
    window.findChild(ScheduleChangeDialog)._submit()
    if action == "tab":
        window._set_view(TaskView.ALL)
    elif action == "calendar":
        window._show_calendar()
    elif action == "close":
        next(
            b for b in window._schedule_result_bar.findChildren(QPushButton) if b.text() == "닫기"
        ).click()
    elif action == "undo":
        window._undo_schedule_move()
        assert tasks.get(task.id).starts_at is None
    else:
        window.shutdown()
    assert not window._schedule_result_bar.isVisible()
    assert not window._schedule_result_timer.isActive()
    assert window._schedule_undo is None


def test_move_date_button_keeps_destination_when_dismissing(workflow_window):
    window, tasks = workflow_window
    task = tasks.create(TaskDraft("날짜 보기"), now=NOW)
    window._open_schedule_change(task, tomorrow=True)
    window.findChild(ScheduleChangeDialog)._submit()
    day = window._schedule_result_day
    window._schedule_date_button.click()
    assert window._calendar_active and window._calendar_page.selected_date == day
    assert not window._schedule_result_bar.isVisible()


def test_second_move_restarts_notice_timeout(workflow_window, qtbot):
    window, tasks = workflow_window
    first = tasks.create(TaskDraft("첫 이동"), now=NOW)
    second = tasks.create(TaskDraft("새 이동"), now=NOW)
    window._open_schedule_change(first, tomorrow=True)
    window.findChild(ScheduleChangeDialog)._submit()
    window._schedule_result_timer.setInterval(80)
    qtbot.wait(20)
    window._open_schedule_change(second, tomorrow=True)
    next(d for d in window.findChildren(ScheduleChangeDialog) if d.isVisible())._submit()
    qtbot.wait(120)
    assert window._schedule_result_bar.isVisible()
    assert window._schedule_result_timer.interval() == 8000
    assert window._schedule_undo.before.id == second.id


def test_deleted_selection_is_not_replaced_by_another_task(workflow_window, qtbot):
    window, tasks = workflow_window
    task = tasks.create(TaskDraft("삭제 전"), now=NOW)
    tasks.create(TaskDraft("남은 업무"), now=NOW)
    window._set_view(TaskView.ALL)
    window._select_task_for_action(task)
    window._set_view(TaskView.UPCOMING)
    tasks.move_to_trash(task.id, now=NOW)
    window._set_view(TaskView.ALL)
    qtbot.wait(20)
    assert window._selected_task_id is None


def test_undo_will_not_overwrite_later_task_edit(workflow_window):
    window, tasks = workflow_window
    task = tasks.create(TaskDraft("이동 후 변경"), now=NOW)
    window._open_schedule_change(task, tomorrow=True)
    window.findChild(ScheduleChangeDialog)._submit()
    current = tasks.get(task.id)
    tasks.update(
        task.id,
        TaskDraft(
            "다른 창에서 새 내용",
            starts_at=current.starts_at,
            ends_at=current.ends_at,
            all_day=current.all_day,
        ),
        now=NOW + timedelta(seconds=1),
    )
    window._undo_schedule_move()
    assert "되돌리지 못했습니다" in window._schedule_feedback.full_text
    assert not window._schedule_result_timer.isActive()
    assert tasks.get(task.id).starts_at == current.starts_at
    assert tasks.get(task.id).title == "다른 창에서 새 내용"


def test_edit_between_move_commit_and_feedback_cannot_be_undone(workflow_window, monkeypatch):
    window, tasks = workflow_window
    task = tasks.create(TaskDraft("동시 변경 보호"), now=NOW)
    service = window._task_save_service
    original = service.move_schedule

    def interleaved(*args, **kwargs):
        saved = original(*args, **kwargs)
        tasks.update(
            task.id,
            TaskDraft(
                "이동 직후 다른 수정",
                starts_at=saved.starts_at,
                ends_at=saved.ends_at,
                all_day=saved.all_day,
            ),
            now=NOW + timedelta(seconds=1),
        )
        return saved

    monkeypatch.setattr(service, "move_schedule", interleaved)
    window._open_schedule_change(task, tomorrow=True)
    window.findChild(ScheduleChangeDialog)._submit()
    window._undo_schedule_move()
    assert tasks.get(task.id).title == "이동 직후 다른 수정"
    assert tasks.get(task.id).starts_at is not None
    assert "되돌리지 못했습니다" in window._schedule_feedback.full_text


def test_lost_ack_retry_cannot_offer_undo_of_later_edits(workflow_window, monkeypatch):
    window, tasks = workflow_window
    task = tasks.create(TaskDraft("재시도 보호"), now=NOW)
    service = window._task_save_service
    original = service.move_schedule
    first = True

    def lost_ack(*args, **kwargs):
        nonlocal first
        saved = original(*args, **kwargs)
        if first:
            first = False
            tasks.update(
                task.id,
                TaskDraft(
                    "저장 후 새 내용",
                    starts_at=saved.starts_at,
                    ends_at=saved.ends_at,
                    all_day=saved.all_day,
                ),
                now=NOW + timedelta(seconds=1),
            )
            raise OSError("응답 유실")
        return saved

    monkeypatch.setattr(service, "move_schedule", lost_ack)
    window._open_schedule_change(task, tomorrow=True)
    dialog = window.findChild(ScheduleChangeDialog)
    dialog._submit()
    assert "응답 유실" in dialog.error.text()
    dialog._submit()
    assert not dialog.isVisible()
    assert "이미 처리된 요청" in window._schedule_feedback.full_text
    assert window._schedule_undo is None and not window._schedule_undo_button.isEnabled()
    assert tasks.get(task.id).title == "저장 후 새 내용"


def test_calendar_detour_and_shutdown_cancel_return(workflow_window, qtbot):
    window, tasks = workflow_window
    task = tasks.create(TaskDraft("달력 다녀오기"), now=NOW)
    window._set_view(TaskView.ALL)
    window._select_task_for_action(task)
    window._show_calendar()
    window._set_view(TaskView.ALL)
    qtbot.wait(20)
    assert window._selected_task_id == task.id
    window._set_view(TaskView.UPCOMING)
    window._set_view(TaskView.ALL)
    assert window._return_timer.isActive()
    window.shutdown()
    assert not window._return_timer.isActive() and window._return_location is None


def test_new_day_and_sort_do_not_restore_old_selection(workflow_window, qtbot):
    from officeflow.application.tasks import TaskSort

    window, tasks = workflow_window
    task = tasks.quick_add("오늘", view=TaskView.TODAY, local_date=NOW.date(), now=NOW)
    window._set_view(TaskView.TODAY)
    window._select_task_for_action(task)
    window._set_view(TaskView.ALL)
    window._view_clock = lambda: NOW + timedelta(days=1)
    window._set_view(TaskView.TODAY)
    qtbot.wait(20)
    assert window._selected_task_id is None
    window._set_view(TaskView.ALL)
    window._select_task_for_action(task)
    window._sort_combo.setCurrentIndex(window._sort_combo.findData(TaskSort.TITLE.value))
    assert TaskView.ALL not in window._view_locations


def test_result_error_keeps_text_and_unsaved_feedback(qtbot, monkeypatch):
    tasks, records = make_dialog_services()
    task = tasks.create(TaskDraft("실패 보존"))
    dialog = TaskRecordsDialog(task, task_service=tasks, record_service=records)
    qtbot.addWidget(dialog)
    dialog.result_edit.setPlainText("보존할 초안")

    def fail(*_args, **_kwargs):
        raise ValueError("다른 창 변경")

    messages = []
    monkeypatch.setattr(tasks, "update_result_note", fail)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: messages.append(_args))
    dialog._save_result()
    assert messages and dialog.result_edit.toPlainText() == "보존할 초안"
    assert "미저장" in dialog.result_save_feedback.full_text


def test_deep_location_is_bounded_without_loading_entire_list(workflow_window, qtbot):
    window, tasks = workflow_window
    for number in range(520):
        tasks.create(TaskDraft(f"긴 목록 {number:03}"), now=NOW + timedelta(seconds=number))
    window._set_view(TaskView.ALL)
    while window._task_model.canFetchMore():
        window._task_model.fetchMore(QModelIndex())
    index = window._task_model.index(515, 0)
    window._task_list.setCurrentIndex(index)
    window._task_list.scrollTo(index)
    qtbot.wait(20)
    window._set_view(TaskView.UPCOMING)
    location = window._view_locations[TaskView.ALL]
    assert location.loaded == 500 and location.truncated
    window._set_view(TaskView.ALL)
    qtbot.wait(30)
    assert window._selected_task_id is None
    assert window._task_model.loaded_task_count == 500
    assert window._task_list.verticalScrollBar().value() == 0


def test_today_cap_is_per_group_not_sum_of_groups(workflow_window, monkeypatch):
    from officeflow.application.tasks import TaskGroup

    window, _tasks = workflow_window
    window._set_view(TaskView.TODAY)
    monkeypatch.setattr(type(window._task_model), "loaded_task_count", property(lambda _self: 600))
    monkeypatch.setattr(
        type(window._task_model),
        "loaded_group_counts",
        property(lambda _self: {TaskGroup.IN_PROGRESS: 300, TaskGroup.COMPLETED: 300}),
    )
    window._remember_list_location()
    assert not window._view_locations[TaskView.TODAY].truncated
    assert window._view_locations[TaskView.TODAY].groups[TaskGroup.IN_PROGRESS] == 300


@pytest.mark.parametrize("dark", [False, True])
def test_narrow_move_receipt_buttons_remain_accessible(workflow_window, qtbot, qapp, dark):
    from PySide6.QtGui import QColor, QPalette

    original = QPalette(qapp.palette())
    palette = QPalette(original)
    if dark:
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#202020"))
    qapp.setPalette(palette)
    window, tasks = workflow_window
    try:
        task = tasks.create(TaskDraft("긴 제목으로 날짜 이동 결과 확인 " * 8), now=NOW)
        window._open_schedule_change(task, tomorrow=True)
        window.findChild(ScheduleChangeDialog)._submit()
        qtbot.wait(40)
        bar = window._schedule_result_bar
        for button in (window._schedule_date_button, window._schedule_undo_button):
            assert bar.rect().contains(button.mapTo(bar, button.rect().bottomRight()))
        assert window._schedule_feedback.width() >= 40
        from officeflow.presentation.dates import format_date

        assert format_date(window._schedule_result_day) in window._schedule_feedback.text()
        assert window.grab().save(
            f"artifacts/continuity-main-{dark}-{window.devicePixelRatioF()}.png"
        )
    finally:
        qapp.setPalette(original)


@pytest.mark.parametrize("dark", [False, True])
def test_small_date_dialog_palette_and_save_buttons(qtbot, qapp, dark):
    from PySide6.QtGui import QColor, QPalette

    original = QPalette(qapp.palette())
    palette = QPalette(original)
    if dark:
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#202020"))
    qapp.setPalette(palette)
    dialog = ScheduleChangeDialog(
        title="기간을 유지하는 날짜 이동",
        starts_at=NOW + timedelta(days=1),
        ends_at=NOW + timedelta(days=1, hours=2),
        all_day=False,
        timezone="Asia/Seoul",
        target=(NOW + timedelta(days=2)).date(),
        today=NOW.date(),
        recurring=True,
        absolute_count=2,
        save=lambda *_a: None,
    )
    qtbot.addWidget(dialog)
    try:
        dialog.resize(460, 320)
        dialog.show()
        qtbot.wait(45)
        for button in dialog.buttons.buttons():
            assert dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight()))
            assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 12
        assert dialog.palette().color(QPalette.ColorRole.Window).lightness() > 220
        assert dialog.grab().save(
            f"artifacts/continuity-move-{dark}-{dialog.devicePixelRatioF()}.png"
        )
    finally:
        qapp.setPalette(original)
