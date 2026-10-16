from datetime import UTC, date, datetime, timedelta

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette

from officeflow.application.tasks import (
    TaskDraft,
    TaskGroup,
    TaskQuery,
    TaskService,
    TaskSort,
    TaskView,
)
from officeflow.infrastructure.database.reminder_repository import SqlAlchemyReminderRepository
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow
from officeflow.presentation.record_dialog import TaskRecordsDialog
from tests.integration.test_attachment_search import search_environment
from tests.unit.test_task_service import InMemoryTaskRepository

NOW = datetime(2026, 10, 16, 3, tzinfo=UTC)


@pytest.fixture
def quick_window(qtbot, tmp_path):
    with search_environment(tmp_path) as env:
        window = MainWindow(
            AppSettings(), env.tasks, record_service=env.records, view_clock=lambda: NOW
        )
        qtbot.addWidget(window)
        window.resize(760, 560)
        window.show()
        try:
            yield window, env.tasks
        finally:
            window.shutdown()


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter])
def test_upcoming_enter_keeps_view_and_reveals_tomorrow(quick_window, qtbot, key):
    window, service = quick_window
    service.create(TaskDraft("먼 일정", starts_at=NOW + timedelta(days=30)), now=NOW)
    window._set_view(TaskView.UPCOMING)
    assert "내일" in window._quick_add_hint.full_text
    assert "10.17(토)" in window._quick_add_hint.full_text
    assert "알림 없음" in window._quick_add_hint.full_text
    window._quick_add_edit.setFocus()
    window._quick_add_edit.setText("내일 할 일")
    qtbot.keyClick(window._quick_add_edit, key)
    qtbot.wait(30)
    saved = service.get(window._selected_task_id)
    assert saved.starts_at == datetime(2026, 10, 16, 15, tzinfo=UTC)
    assert saved.ends_at == datetime(2026, 10, 17, 15, tzinfo=UTC)
    assert saved.all_day and window._current_view is TaskView.UPCOMING
    assert (
        SqlAlchemyReminderRepository(service._repository._sessions).list_reminders(saved.id) == ()
    )
    assert window._task_model.task_at(window._task_model.index(0, 0)).id == saved.id
    assert window._task_list.currentIndex() == window._task_model.index_for_task(saved.id)
    assert window._quick_add_edit.hasFocus()
    assert "등록됨" in window._quick_add_hint.full_text
    assert "내일 할 일" in window._quick_add_hint.full_text
    assert window._quick_add_edit.text() == ""


def test_today_collapsed_group_is_expanded_and_saved_receipt_stays(quick_window, qtbot):
    window, service = quick_window
    service.quick_add("기존", view=TaskView.TODAY, local_date=date(2026, 10, 16), now=NOW)
    window._set_view(TaskView.TODAY)
    window._collapsed_groups.add(TaskGroup.IN_PROGRESS)
    window._task_model.toggle_group(TaskGroup.IN_PROGRESS)
    window._quick_add_edit.setText("새 제목")
    window._quick_add_task()
    assert window._task_model.index_for_task(window._selected_task_id).isValid()
    assert "새 제목" in window._quick_add_hint.full_text
    qtbot.wait(40)
    window._refresh_tasks(preserve_state=True)
    assert "등록됨" in window._quick_add_hint.full_text
    window._quick_add_edit.setText("다음 제목")
    assert "등록됨" not in window._quick_add_hint.full_text
    assert "오늘" in window._quick_add_hint.full_text


def test_filtered_saved_task_stays_confirmed_without_clearing_search(quick_window):
    window, service = quick_window
    window._set_view(TaskView.UPCOMING)
    window._search.setText("일치하지 않는 검색")
    window._search_timer.stop()
    window._quick_add_edit.setText("새 업무")
    window._quick_add_task()
    saved = window._quick_add_saved
    assert saved is not None and service.get(saved.id).title == "새 업무"
    assert window._current_view is TaskView.UPCOMING
    assert window._search.text() == "일치하지 않는 검색"
    assert window._task_model.total_task_count == 0
    assert "등록됨" in window._quick_add_hint.full_text
    assert "목록 밖" in window._quick_add_hint.full_text
    assert not window._quick_add_open_button.isHidden()
    window._search.clear()
    window._search_timer.stop()
    window._refresh_tasks()
    assert "목록 밖" not in window._quick_add_hint.full_text


def test_off_page_saved_task_does_not_read_entire_list(quick_window, monkeypatch, qtbot):
    window, service = quick_window
    for number in range(115):
        service.create(TaskDraft(f"A {number:03}", starts_at=NOW + timedelta(days=2)), now=NOW)
    window._set_view(TaskView.ALL)
    calls = []
    original = service.query

    def query(value, **kwargs):
        calls.append(value)
        return original(value, **kwargs)

    monkeypatch.setattr(service, "query", query)
    window._quick_add_edit.setText("Z 새 업무")
    window._quick_add_task()
    qtbot.wait(30)
    assert window._task_model.loaded_task_count <= 100
    assert all(value.limit is not None and value.limit <= 100 for value in calls)
    assert len(calls) <= 3
    assert window._quick_add_saved is not None
    assert "등록됨" in window._quick_add_hint.full_text
    assert "목록 밖" in window._quick_add_hint.full_text


def test_loaded_task_is_scrolled_into_view_without_changing_sort(quick_window, qtbot):
    window, service = quick_window
    for number in range(35):
        service.create(TaskDraft(f"A {number:03}"), now=NOW)
    window._set_view(TaskView.ALL)
    window._set_combo_value(window._sort_combo, TaskSort.TITLE.value)
    window._task_list.scrollToTop()
    window._quick_add_edit.setText("Z 마지막 업무")
    window._quick_add_task()
    qtbot.wait(50)
    index = window._task_model.index_for_task(window._selected_task_id)
    assert index.isValid()
    assert window._task_list.viewport().rect().intersects(window._task_list.visualRect(index))
    assert window._sort_combo.currentData() == TaskSort.TITLE.value


@pytest.mark.parametrize("view", [TaskView.IMPORTANT, TaskView.PENDING, TaskView.COMPLETED])
def test_other_tab_transfer_is_disclosed_in_advance(quick_window, view):
    window, service = quick_window
    window._set_view(view)
    assert "전체 업무" in window._quick_add_hint.full_text
    assert "일정 없음" in window._quick_add_hint.full_text
    window._quick_add_edit.setText("다른 탭 등록")
    window._quick_add_task()
    assert window._current_view is TaskView.ALL
    assert service.get(window._selected_task_id).starts_at is None
    assert "등록됨" in window._quick_add_hint.full_text


def test_failure_keeps_input_and_no_success_receipt(quick_window, monkeypatch):
    window, service = quick_window
    window._set_view(TaskView.UPCOMING)
    errors = []
    monkeypatch.setattr(window, "_show_error", lambda *args: errors.append(args))

    def fail(*args, **kwargs):
        raise OSError("합성 저장 실패")

    monkeypatch.setattr(service, "quick_add", fail)
    window._quick_add_edit.setText("보존할 제목")
    window._quick_add_task()
    assert errors and window._quick_add_edit.text() == "보존할 제목"
    assert window._quick_add_saved is None
    assert "등록됨" not in window._quick_add_hint.full_text


@pytest.mark.parametrize(
    ("timezone", "day", "hours"),
    [("Asia/Seoul", date(2026, 12, 31), 24), ("America/New_York", date(2026, 3, 7), 23)],
)
def test_upcoming_day_end_follows_local_calendar(timezone, day, hours):
    service = TaskService(InMemoryTaskRepository(), timezone=timezone)
    task = service.quick_add("내일", view=TaskView.UPCOMING, local_date=day, now=NOW)
    assert task.starts_at is not None and task.ends_at is not None
    assert (task.ends_at - task.starts_at).total_seconds() / 3600 == hours
    from zoneinfo import ZoneInfo

    assert task.starts_at.astimezone(ZoneInfo(timezone)).date() == day + timedelta(days=1)
    assert task.all_day


def test_receipt_opens_saved_identity_not_changed_selection(quick_window, qtbot):
    window, service = quick_window
    other = service.create(TaskDraft("다른 업무"), now=NOW)
    window._set_view(TaskView.UPCOMING)
    window._quick_add_edit.setText("확인할 등록 업무")
    window._quick_add_task()
    saved = window._quick_add_saved
    window._selected_task_id = other.id
    qtbot.mouseClick(window._quick_add_open_button, Qt.MouseButton.LeftButton)
    dialogs = [item for item in window._file_dialogs if isinstance(item, TaskRecordsDialog)]
    assert len(dialogs) == 1 and dialogs[0]._task_id == saved.id
    assert service.get(saved.id) == saved


def test_receipt_does_not_open_deleted_task(quick_window):
    window, service = quick_window
    window._quick_add_edit.setText("이후 삭제")
    window._quick_add_task()
    service.move_to_trash(window._quick_add_saved.id, now=NOW)
    window._open_quick_added_task()
    assert not window._file_dialogs
    assert "열 수 없습니다" in window.statusBar().currentMessage()


def test_registration_date_and_preview_follow_midnight(quick_window):
    window, service = quick_window
    clock = [datetime(2026, 12, 31, 14, 59, tzinfo=UTC)]
    window._view_clock = lambda: clock[0]
    window._set_view(TaskView.UPCOMING)
    assert "01.01(금)" in window._quick_add_hint.full_text
    clock[0] += timedelta(minutes=2)
    window._refresh_tasks()
    assert "01.02(토)" in window._quick_add_hint.full_text
    window._quick_add_edit.setText("자정 이후")
    window._quick_add_task()
    task = service.get(window._quick_add_saved.id)
    assert task.starts_at == datetime(2027, 1, 1, 15, tzinfo=UTC)
    assert task.created_at == clock[0]


def test_retry_after_committed_save_does_not_duplicate_task(quick_window, monkeypatch):
    window, service = quick_window
    original = service.quick_add
    calls = []

    def interrupted(*args, **kwargs):
        saved = original(*args, **kwargs)
        calls.append(kwargs["command_id"])
        if len(calls) == 1:
            raise OSError("저장 직후 응답 실패")
        return saved

    monkeypatch.setattr(service, "quick_add", interrupted)
    monkeypatch.setattr(window, "_show_error", lambda *args: None)
    window._set_view(TaskView.UPCOMING)
    window._quick_add_edit.setText("재시도 제목")
    window._quick_add_task()
    assert window._quick_add_edit.text() == "재시도 제목"
    window._quick_add_task()
    assert len(calls) == 2 and calls[0] == calls[1]
    assert service.query(TaskQuery(), now=NOW).total == 1
    assert "등록됨" in window._quick_add_hint.full_text


def test_service_default_day_uses_supplied_now_not_host_time():
    service = TaskService(InMemoryTaskRepository(), timezone="Asia/Seoul")
    task = service.quick_add(
        "정해진 시계", view=TaskView.UPCOMING, now=datetime(2030, 12, 31, 16, tzinfo=UTC)
    )
    assert task.starts_at == datetime(2031, 1, 1, 15, tzinfo=UTC)


@pytest.mark.parametrize("dark", [False, True])
def test_small_window_preview_and_receipt_visual(quick_window, qtbot, qapp, dark):
    window, _service = quick_window
    original = QPalette(qapp.palette())
    palette = QPalette(original)
    if dark:
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#202020"))
    qapp.setPalette(palette)
    try:
        window._set_view(TaskView.UPCOMING)
        qtbot.wait(30)
        before = window._quick_add_context.height()
        assert window.grab().save(
            f"artifacts/quick-preview-{dark}-{window.devicePixelRatioF()}.png"
        )
        window._quick_add_edit.setText("제목이 긴 새 업무 " * 8)
        window._quick_add_task()
        qtbot.wait(40)
        assert window._quick_add_context.height() == before
        assert (
            window._quick_add_hint.palette().color(QPalette.ColorRole.WindowText).lightness() < 160
        )
        assert window._quick_add_hint.toolTip() == window._quick_add_hint.full_text
        assert window.rect().contains(
            window._quick_add_open_button.mapTo(
                window, window._quick_add_open_button.rect().bottomRight()
            )
        )
        assert window._quick_add_edit.width() > 180
        assert window.grab().save(
            f"artifacts/quick-receipt-{dark}-{window.devicePixelRatioF()}.png"
        )
    finally:
        qapp.setPalette(original)
