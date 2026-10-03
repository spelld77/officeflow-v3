from datetime import UTC, datetime, timedelta

from PySide6.QtCore import QItemSelectionModel

from officeflow.application.tasks import TaskDraft, TaskGroup, TaskService, TaskView
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow
from tests.unit.test_task_service import InMemoryTaskRepository


def test_midnight_and_sleep_return_refresh_today_without_reminders(qtbot):
    clock = [datetime(2026, 10, 2, 14, 59, tzinfo=UTC)]
    service = TaskService(InMemoryTaskRepository())
    yesterday = service.create(TaskDraft(title="전날", starts_at=clock[0] - timedelta(hours=1)))
    service.create(TaskDraft(title="새날", starts_at=clock[0] + timedelta(hours=10)))
    window = MainWindow(AppSettings(), service, view_clock=lambda: clock[0])
    qtbot.addWidget(window)
    assert window._view_timer.isActive()
    assert window._page_title.text() == "오늘 · 10월 2일"
    window.show()
    clock[0] += timedelta(hours=10)
    window._view_timer.timeout.emit()
    assert window._page_title.text() == "오늘 · 10월 3일"
    assert window._task_model.total_task_count == 2
    assert window._task_model.loaded_group_counts[TaskGroup.OVERDUE] == 1
    assert window._task_model.index_for_task(yesterday.id).isValid()
    window._check_reminders()
    assert window._page_title.text() == "오늘 · 10월 3일"
    window.shutdown()
    assert not window._view_timer.isActive()


def test_refresh_preserves_search_filters_loaded_pages_selection_and_scroll(qtbot):
    clock = [datetime(2026, 10, 3, 1, tzinfo=UTC)]
    service = TaskService(InMemoryTaskRepository())
    for number in range(125):
        service.create(
            TaskDraft(title=f"검토 {number:03}", starts_at=clock[0] + timedelta(hours=2))
        )
    window = MainWindow(AppSettings(), service, view_clock=lambda: clock[0])
    qtbot.addWidget(window)
    window.show()
    window._search.setText("검토")
    window._search_timer.stop()
    window._refresh_tasks()
    window._task_model.load_more(TaskGroup.IN_PROGRESS)
    assert window._task_model.loaded_group_counts[TaskGroup.IN_PROGRESS] == 100
    task = window._task_model.task_at(window._task_model.index(90, 0))
    assert task is not None
    window._task_list.setCurrentIndex(window._task_model.index_for_task(task.id))
    window._task_list.verticalScrollBar().setValue(20)
    before_scroll = window._task_list.verticalScrollBar().value()
    before_collapsed = window._collapsed_groups.copy()
    clock[0] += timedelta(minutes=2)
    window._refresh_time_sensitive_view()
    assert window._search.text() == "검토"
    assert window._collapsed_groups == before_collapsed
    assert window._task_model.loaded_group_counts[TaskGroup.IN_PROGRESS] == 100
    assert window._selected_task_id == task.id
    assert window._task_list.verticalScrollBar().value() == before_scroll
    window.shutdown()


def test_minute_refresh_reclassifies_due_tasks_and_coalesces_same_minute(qtbot):
    clock = [datetime(2026, 10, 3, 1, tzinfo=UTC)]
    repo = InMemoryTaskRepository()
    service = TaskService(repo)
    service.create(TaskDraft(title="경계", starts_at=clock[0] + timedelta(minutes=1)))
    window = MainWindow(AppSettings(), service, view_clock=lambda: clock[0])
    qtbot.addWidget(window)
    before = len(repo.queries)
    window._refresh_time_sensitive_view()
    assert len(repo.queries) == before
    clock[0] += timedelta(minutes=2)
    window._refresh_time_sensitive_view()
    assert window._task_model.loaded_group_counts[TaskGroup.OVERDUE] == 1
    before = len(repo.queries)
    window._refresh_time_sensitive_view()
    assert len(repo.queries) == before
    window.shutdown()


def test_clock_backwards_refresh_and_calendar_date_are_preserved(qtbot):
    clock = [datetime(2026, 10, 3, 1, tzinfo=UTC)]
    window = MainWindow(
        AppSettings(), TaskService(InMemoryTaskRepository()), view_clock=lambda: clock[0]
    )
    qtbot.addWidget(window)
    window._show_calendar()
    selected = window._calendar_page.selected_date
    month = window._calendar_page.calendar.displayed_month
    clock[0] -= timedelta(days=1)
    window._refresh_time_sensitive_view()
    assert window._calendar_page.selected_date == selected
    assert window._calendar_page.calendar.displayed_month == month
    window._set_view(TaskView.TODAY)
    assert window._page_title.text() == "오늘 · 10월 2일"
    window.shutdown()


def test_refresh_keeps_more_than_500_loaded_items_and_multiple_selection(qtbot):
    clock = [datetime(2026, 10, 3, 1, tzinfo=UTC)]
    repo = InMemoryTaskRepository()
    service = TaskService(repo)
    for number in range(625):
        service.create(
            TaskDraft(title=f"대량 {number:03}", starts_at=clock[0] + timedelta(hours=1))
        )
    window = MainWindow(AppSettings(), service, view_clock=lambda: clock[0])
    qtbot.addWidget(window)
    for _ in range(11):
        window._task_model.load_more(TaskGroup.IN_PROGRESS)
    assert window._task_model.loaded_group_counts[TaskGroup.IN_PROGRESS] == 600
    indexes = [window._task_model.index(row, 0) for row in (550, 575)]
    window._task_list.setCurrentIndex(indexes[0])
    window._task_list.selectionModel().select(indexes[1], QItemSelectionModel.SelectionFlag.Select)
    selected = {
        window._task_model.task_at(index).id
        for index in window._task_list.selectionModel().selectedIndexes()
    }
    clock[0] += timedelta(minutes=1)
    window._refresh_time_sensitive_view()
    assert window._task_model.loaded_group_counts[TaskGroup.IN_PROGRESS] == 600
    assert {
        window._task_model.task_at(index).id
        for index in window._task_list.selectionModel().selectedIndexes()
    } == selected
    assert all(query.limit is None or query.limit <= 500 for query in repo.queries)
    window.shutdown()


def test_loaded_task_time_boundary_refreshes_even_inside_same_minute(qtbot):
    clock = [datetime(2026, 10, 3, 1, 0, 5, tzinfo=UTC)]
    service = TaskService(InMemoryTaskRepository())
    service.create(TaskDraft(title="초 단위 경계", starts_at=clock[0] + timedelta(seconds=10)))
    window = MainWindow(AppSettings(), service, view_clock=lambda: clock[0])
    qtbot.addWidget(window)
    assert window._task_model.loaded_group_counts[TaskGroup.OVERDUE] == 0
    clock[0] += timedelta(seconds=30)
    window._refresh_time_sensitive_view()
    assert window._task_model.loaded_group_counts[TaskGroup.OVERDUE] == 1
    window.shutdown()


def test_activating_window_after_hidden_interval_refreshes_immediately(qtbot):
    clock = [datetime(2026, 10, 2, 14, 59, tzinfo=UTC)]
    window = MainWindow(
        AppSettings(), TaskService(InMemoryTaskRepository()), view_clock=lambda: clock[0]
    )
    qtbot.addWidget(window)
    clock[0] += timedelta(days=1)
    window._view_timer.timeout.emit()
    assert window._page_title.text() == "오늘 · 10월 2일"
    window.show()
    window.activateWindow()
    qtbot.waitUntil(lambda: window._page_title.text() == "오늘 · 10월 3일", timeout=2000)
    window.shutdown()
