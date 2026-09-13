from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QLineEdit, QListView, QPushButton, QWidget
from pytestqt.qtbot import QtBot

from officeflow.application.tasks import TaskDraft, TaskGroup, TaskService, TaskView
from officeflow.domain.enums import TaskPriority, TaskStatus
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow
from officeflow.presentation.task_list import GroupHeader


def test_main_window_has_phase_two_shell(qtbot: QtBot, task_service: TaskService) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.show()

    assert window.windowTitle() == "OfficeFlow v3"
    assert window.minimumWidth() == 760
    assert window.findChild(type(window.centralWidget()), "appRoot") is not None


def test_wide_window_shows_three_panels(qtbot: QtBot, task_service: TaskService) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.resize(1280, 800)
    window.show()
    qtbot.wait(10)

    detail = window.findChild(QWidget, "detailPanel")
    sidebar = window.findChild(QWidget, "sidebar")
    assert detail is not None and detail.isVisible()
    assert sidebar is not None and sidebar.width() == 212


def test_medium_window_prioritizes_task_list(qtbot: QtBot, task_service: TaskService) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.resize(960, 640)
    window.show()
    qtbot.wait(10)

    detail = window.findChild(QWidget, "detailPanel")
    sidebar = window.findChild(QWidget, "sidebar")
    assert detail is not None and detail.isHidden()
    assert sidebar is not None and sidebar.width() == 180
    assert window._summary_layout.getItemPosition(3)[:2] == (0, 3)


def test_compact_window_uses_small_navigation_and_wrapped_summaries(
    qtbot: QtBot, task_service: TaskService
) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.resize(760, 560)
    window.show()
    qtbot.wait(10)

    sidebar = window.findChild(QWidget, "sidebar")
    detail = window.findChild(QWidget, "detailPanel")
    assert sidebar is not None and sidebar.width() == 88
    assert detail is not None and detail.isHidden()
    assert window._summary_layout.getItemPosition(2)[:2] == (1, 0)


def test_window_geometry_is_saved_on_close(qtbot: QtBot, task_service: TaskService) -> None:
    saved: list[AppSettings] = []
    window = MainWindow(AppSettings(), task_service, save_settings=saved.append)
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    qtbot.wait(10)

    window.close()

    assert saved
    assert saved[-1].window_width == 900
    assert saved[-1].window_height == 600
    assert saved[-1].window_x is not None
    assert saved[-1].window_y is not None


def test_quick_add_creates_today_task(qtbot: QtBot, task_service: TaskService) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.show()

    quick_add = window.findChild(QLineEdit, "quickAddEdit")
    assert quick_add is not None
    qtbot.keyClicks(quick_add, "Write phase two tests")
    qtbot.keyPress(quick_add, Qt.Key.Key_Return)

    task_list = window.findChild(QListView, "taskList")
    assert task_list is not None
    assert window._task_model.total_task_count == 1
    assert window._task_model.index_for_group(TaskGroup.IN_PROGRESS).isValid()


def test_tab_moves_from_quick_add_input_to_add_button(
    qtbot: QtBot, task_service: TaskService
) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.show()
    quick_add = window.findChild(QLineEdit, "quickAddEdit")
    quick_add_button = window.findChild(QPushButton, "quickAddButton")
    assert quick_add is not None
    assert quick_add_button is not None
    quick_add.setFocus()

    qtbot.keyPress(quick_add, Qt.Key.Key_Tab)

    assert window.focusWidget() is quick_add_button


def test_today_view_has_collapsible_groups_and_summary_jump(
    qtbot: QtBot, task_service: TaskService
) -> None:
    now = datetime.now(UTC)
    task_service.create(
        TaskDraft(
            title="완료한 업무",
            status=TaskStatus.COMPLETED,
            starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(hours=1),
        ),
        now=now,
    )
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.show()

    header = window._task_model.entry_at(window._task_model.index_for_group(TaskGroup.COMPLETED))
    assert isinstance(header, GroupHeader)
    assert header.collapsed is True

    jump = window.findChild(QPushButton, "summaryJump-completed")
    assert jump is not None
    qtbot.mouseClick(jump, Qt.MouseButton.LeftButton)

    expanded = window._task_model.entry_at(window._task_model.index_for_group(TaskGroup.COMPLETED))
    assert isinstance(expanded, GroupHeader)
    assert expanded.collapsed is False


def test_filters_combine_and_can_be_cleared(qtbot: QtBot, task_service: TaskService) -> None:
    task_service.create(TaskDraft(title="긴급 고정", priority=TaskPriority.URGENT, is_pinned=True))
    task_service.create(TaskDraft(title="일반 업무", priority=TaskPriority.NORMAL))
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window._set_view(TaskView.ALL)
    priority = window.findChild(QComboBox, "priorityFilter")
    pinned = window.findChild(QPushButton, "pinnedFilter")
    clear = window.findChild(QPushButton, "clearTaskFilters")
    assert priority is not None
    assert pinned is not None
    assert clear is not None

    priority.setCurrentIndex(priority.findData(TaskPriority.URGENT.value))
    pinned.setChecked(True)

    assert window._task_model.total_task_count == 1
    assert clear.isEnabled()
    qtbot.mouseClick(clear, Qt.MouseButton.LeftButton)
    assert window._task_model.total_task_count == 2


def test_flat_view_loads_fifty_rows_then_fetches_more(
    qtbot: QtBot, task_service: TaskService
) -> None:
    for index in range(120):
        task_service.create(TaskDraft(title=f"대량 업무 {index:03d}"))
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)

    window._set_view(TaskView.ALL)

    assert window._task_model.loaded_task_count == 50
    assert window._task_model.total_task_count == 120
    assert window._task_model.canFetchMore()
    window._task_model.fetchMore()
    assert window._task_model.loaded_task_count == 100


def test_view_preferences_and_compact_mode_are_saved(
    qtbot: QtBot, task_service: TaskService
) -> None:
    saved: list[AppSettings] = []
    window = MainWindow(AppSettings(), task_service, save_settings=saved.append)
    qtbot.addWidget(window)
    window._set_view(TaskView.ALL)
    status = window.findChild(QComboBox, "statusFilter")
    compact = window.findChild(QPushButton, "compactListToggle")
    assert status is not None
    assert compact is not None
    status.setCurrentIndex(status.findData(TaskStatus.ACTIVE.value))
    compact.setChecked(True)
    window.close()

    assert saved[-1].compact_list is True
    assert saved[-1].view_preferences[TaskView.ALL.value]["status"] == "active"


def test_calendar_navigation_loads_scheduled_tasks_and_day_list(
    qtbot: QtBot, task_service: TaskService
) -> None:
    zone = ZoneInfo("Asia/Seoul")
    today = datetime.now(zone).date()
    start = datetime.combine(today, time.min, tzinfo=zone).astimezone(UTC)
    task_service.create(
        TaskDraft(
            title="캘린더 연결 확인",
            all_day=True,
            starts_at=start,
            ends_at=start + timedelta(days=2),
        )
    )
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.show()

    window._calendar_button.click()

    assert window._calendar_active is True
    assert window._content_stack.currentWidget() is window._calendar_page
    assert window._calendar_page.day_list.count() == 1
    assert "캘린더 연결 확인" in window._calendar_page.day_list.item(0).text()


def test_calendar_stays_usable_at_minimum_window_size(
    qtbot: QtBot, task_service: TaskService
) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.resize(760, 560)
    window.show()
    window._show_calendar()
    qtbot.wait(10)

    assert window._calendar_page.isVisible()
    assert window._calendar_page.calendar.width() >= 460
    assert window._calendar_page.day_list.isVisible()
    assert window._calendar_page.edit_button.text() == "수정"
    assert (
        window._calendar_page.calendar.geometry().bottom()
        < window._calendar_page.day_list.geometry().top()
    )


def test_calendar_overflow_opens_complete_day_list(qtbot: QtBot, task_service: TaskService) -> None:
    zone = ZoneInfo("Asia/Seoul")
    today = datetime.now(zone).date()
    start = datetime.combine(today, time.min, tzinfo=zone).astimezone(UTC)
    for index in range(6):
        task_service.create(
            TaskDraft(
                title=f"겹친 일정 {index}",
                all_day=True,
                starts_at=start,
                ends_at=start + timedelta(days=1),
            )
        )
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.show()
    window._show_calendar()
    qtbot.wait(10)
    window._calendar_page.calendar.grab()
    overflow = next(rect for rect, day in window._calendar_page.calendar._more_hits if day == today)

    qtbot.mouseClick(
        window._calendar_page.calendar,
        Qt.MouseButton.LeftButton,
        pos=overflow.center().toPoint(),
    )

    assert window._calendar_page.day_list.count() == 6
