from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from PySide6.QtCore import QItemSelectionModel, QPoint, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QLineEdit,
    QListView,
    QMessageBox,
    QPushButton,
    QWidget,
)
from pytestqt.qtbot import QtBot

import officeflow.presentation.main_window as main_window_module
from officeflow.application.attachments import AttachmentService
from officeflow.application.records import RecordService
from officeflow.application.reminders import ReminderService
from officeflow.application.tasks import (
    ScheduledTask,
    TaskDraft,
    TaskGroup,
    TaskService,
    TaskView,
)
from officeflow.domain.enums import (
    OccurrenceStatus,
    ReminderDeliveryStatus,
    ReminderRelation,
    TaskPriority,
    TaskStatus,
)
from officeflow.domain.reminder import ReminderRuleInput
from officeflow.infrastructure.attachments.storage import ManagedAttachmentStorage
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow
from officeflow.presentation.task_list import GroupHeader, TaskListModel
from tests.unit.test_attachment_service import InMemoryAttachmentRepository
from tests.unit.test_record_service import InMemoryRecordRepository
from tests.unit.test_reminder_service import InMemoryReminderRepository
from tests.unit.test_task_service import InMemoryTaskRepository


def test_help_button_opens_packaged_user_guide(
    qtbot: QtBot, task_service: TaskService, monkeypatch
) -> None:
    opened: list[bool] = []
    monkeypatch.setattr(
        main_window_module,
        "open_user_help",
        lambda: opened.append(True) or True,
    )
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)

    window._help_button.click()

    assert opened == [True]
    assert window._version_label.text() == "OfficeFlow 3.0.0"


class FakeTrayIcon:
    def __init__(self) -> None:
        self.visible = True
        self.messages: list[tuple[object, ...]] = []

    def isVisible(self) -> bool:
        return self.visible

    def showMessage(self, *args: object) -> None:
        self.messages.append(args)

    def hide(self) -> None:
        self.visible = False


def test_main_window_has_phase_two_shell(qtbot: QtBot, task_service: TaskService) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.show()

    assert window.windowTitle() == "OfficeFlow v3"
    assert window.minimumWidth() == 760
    assert window.findChild(type(window.centralWidget()), "appRoot") is not None


def test_unclean_shutdown_warning_is_shown_on_startup(
    qtbot: QtBot,
    task_service: TaskService,
    monkeypatch,
) -> None:
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    window = MainWindow(
        AppSettings(),
        task_service,
        previous_unclean_shutdown=True,
    )
    qtbot.addWidget(window)
    window.show()

    qtbot.waitUntil(lambda: bool(warnings), timeout=1_000)

    assert warnings[0][0] == "이전 실행 비정상 종료"
    assert "놓친 알림" in warnings[0][1]


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
    assert window._filter_options.isHidden()
    assert window._filter_toggle.isVisible()
    assert window._result_count.geometry().right() <= window._filter_bar.contentsRect().right()
    assert window._page_caption.isVisible()


def test_medium_window_exposes_records_without_detail_panel(qtbot: QtBot) -> None:
    task_repository = InMemoryTaskRepository()
    task_service = TaskService(task_repository)
    record_service = RecordService(InMemoryRecordRepository(), task_service)
    task_service.create(TaskDraft(title="기록할 업무"))
    window = MainWindow(AppSettings(), task_service, record_service=record_service)
    qtbot.addWidget(window)
    window.resize(960, 640)
    window.show()
    window._set_view(TaskView.ALL)
    window._task_list.setCurrentIndex(window._task_model.index(0, 0))
    qtbot.wait(10)

    records_button = window.findChild(QPushButton, "openSelectedRecordsButton")
    assert records_button is not None and records_button.isVisible()
    assert window._detail_panel.isHidden()
    assert window._work_log_button.isEnabled()


def test_multiple_selected_tasks_are_available_for_calendar_export(qtbot: QtBot) -> None:
    task_service = TaskService(InMemoryTaskRepository())
    first = task_service.create(TaskDraft(title="첫 출장 일정"))
    second = task_service.create(TaskDraft(title="둘째 출장 일정"))
    assert first.id is not None and second.id is not None
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window._set_view(TaskView.ALL)
    first_index = window._task_model.index_for_task(first.id)
    second_index = window._task_model.index_for_task(second.id)
    selection = window._task_list.selectionModel()
    selection.select(first_index, QItemSelectionModel.SelectionFlag.Select)
    selection.select(second_index, QItemSelectionModel.SelectionFlag.Select)

    assert set(window._selected_task_ids_for_export()) == {first.id, second.id}


def test_task_context_menu_completes_with_result(qtbot: QtBot, monkeypatch) -> None:
    task_repository = InMemoryTaskRepository()
    task_service = TaskService(task_repository)
    record_service = RecordService(InMemoryRecordRepository(), task_service)
    task = task_service.create(TaskDraft(title="우클릭 완료"))
    assert task.id is not None
    window = MainWindow(AppSettings(), task_service, record_service=record_service)
    qtbot.addWidget(window)
    window._set_view(TaskView.ALL)
    window._task_list.setCurrentIndex(window._task_model.index_for_task(task.id))

    menu = window._build_task_context_menu(task)
    assert "완료 요약 입력 후 완료…" in [action.text() for action in menu.actions()]
    assert "바로 완료" in [action.text() for action in menu.actions()]

    class FakeCompleteDialog:
        DialogCode = QDialog.DialogCode

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def setStyleSheet(self, _style: str) -> None:
            pass

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        def result_note(self) -> str:
            return "검수까지 완료"

    monkeypatch.setattr(main_window_module, "CompleteTaskDialog", FakeCompleteDialog)
    window._complete_selected_with_result()

    saved = task_service.get(task.id)
    assert saved.status is TaskStatus.COMPLETED
    assert saved.result_note == "검수까지 완료"


def test_task_can_move_to_trash_and_be_restored(
    qtbot: QtBot,
    monkeypatch,
) -> None:
    task_repository = InMemoryTaskRepository()
    task_service = TaskService(task_repository)
    task = task_service.create(TaskDraft(title="휴지통 테스트"))
    assert task.id is not None
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window._set_view(TaskView.ALL)
    window._task_list.setCurrentIndex(window._task_model.index_for_task(task.id))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    actions = [action.text() for action in window._build_task_context_menu(task).actions()]
    assert "휴지통으로 이동" in actions
    window._move_selected_to_trash()

    assert task_service.list(TaskView.ALL) == []
    window._set_view(TaskView.TRASH)
    deleted = window._task_model.task_at(window._task_model.index(0, 0))
    assert deleted is not None and deleted.deleted_at is not None
    assert [
        action.text() for action in window._build_task_context_menu(deleted).actions()
    ] == ["휴지통에서 복원"]

    window._task_list.setCurrentIndex(window._task_model.index_for_task(task.id))
    window._restore_selected_from_trash()

    assert task_service.get(task.id).title == "휴지통 테스트"
    assert task_service.list(TaskView.TRASH) == []


def test_trash_view_exposes_preserved_records_and_attachments(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    task_service = TaskService(InMemoryTaskRepository())
    task = task_service.create(TaskDraft(title="삭제 업무 자료"))
    assert task.id is not None
    record_service = RecordService(InMemoryRecordRepository(), task_service)
    attachment_service = AttachmentService(
        InMemoryAttachmentRepository(),
        ManagedAttachmentStorage(tmp_path / "attachments"),
        task_service,
    )
    task_service.move_to_trash(task.id)
    window = MainWindow(
        AppSettings(),
        task_service,
        record_service=record_service,
        attachment_service=attachment_service,
    )
    qtbot.addWidget(window)
    window._set_view(TaskView.TRASH)
    index = window._task_model.index_for_task(task.id)
    window._task_list.setCurrentIndex(index)
    deleted = window._task_model.task_at(index)
    assert deleted is not None

    labels = [action.text() for action in window._build_task_context_menu(deleted).actions()]

    assert "완료 요약 · 기록 보기" in labels
    assert "첨부파일 보기…" in labels
    assert window._detail_records_button.isEnabled()
    assert window._detail_attachment_button.isEnabled()


def test_quick_complete_can_be_undone(qtbot: QtBot) -> None:
    task_repository = InMemoryTaskRepository()
    task_service = TaskService(task_repository)
    task = task_service.create(TaskDraft(title="빠른 완료"))
    assert task.id is not None
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window._set_view(TaskView.ALL)
    window.show()
    index = window._task_model.index_for_task(task.id)
    rect = window._task_list.visualRect(index)

    qtbot.mouseClick(
        window._task_list.viewport(),
        Qt.MouseButton.LeftButton,
        pos=QPoint(rect.left() + 25, rect.center().y()),
    )

    assert task_service.get(task.id).status is TaskStatus.COMPLETED
    assert window._undo_button.isVisible()
    assert "완료했습니다" in window.statusBar().currentMessage()

    qtbot.mouseClick(window._undo_button, Qt.MouseButton.LeftButton)

    assert task_service.get(task.id).status is TaskStatus.ACTIVE
    assert window._undo_button.isHidden()


def test_quick_complete_undo_restores_pending_status(qtbot: QtBot) -> None:
    task_repository = InMemoryTaskRepository()
    task_service = TaskService(task_repository)
    task = task_service.create(
        TaskDraft(title="대기 업무 완료", status=TaskStatus.PENDING)
    )
    assert task.id is not None
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window._set_view(TaskView.ALL)

    window._quick_complete_task(task)
    window._undo_last_completion()

    assert task_service.get(task.id).status is TaskStatus.PENDING


def test_filter_controls_are_collapsed_but_active_filters_remain_visible(
    qtbot: QtBot, task_service: TaskService
) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.show()

    assert window._filter_options.isHidden()
    qtbot.mouseClick(window._filter_toggle, Qt.MouseButton.LeftButton)
    assert window._filter_options.isVisible()

    window._priority_filter.setCurrentIndex(
        window._priority_filter.findData(TaskPriority.IMPORTANT.value)
    )
    window._filter_toggle.setChecked(False)

    assert window._filter_options.isHidden()
    assert window._filter_toggle.text() == "필터 1개"
    assert window._active_filter_label.text() == "중요"
    assert window._clear_filters_button.isVisible()


def test_attachment_filter_combines_with_task_view(qtbot: QtBot, tmp_path: Path) -> None:
    task_repository = InMemoryTaskRepository()
    task_service = TaskService(task_repository)
    attached = task_service.create(TaskDraft(title="첨부 있음"))
    task_service.create(TaskDraft(title="첨부 없음"))
    assert attached.id is not None
    task_repository.tasks[attached.id] = replace(attached, has_attachments=True)
    attachment_service = AttachmentService(
        InMemoryAttachmentRepository(),
        ManagedAttachmentStorage(tmp_path / "attachments"),
        task_service,
    )
    window = MainWindow(
        AppSettings(),
        task_service,
        attachment_service=attachment_service,
    )
    qtbot.addWidget(window)
    window.show()
    window._set_view(TaskView.ALL)

    window._attachment_filter.setChecked(True)

    assert window._task_model.total_task_count == 1
    assert window._task_model.task_at(window._task_model.index(0, 0)).title == "첨부 있음"


def test_attachment_context_action_opens_management_instead_of_file_picker(
    qtbot: QtBot, tmp_path: Path
) -> None:
    task_repository = InMemoryTaskRepository()
    task_service = TaskService(task_repository)
    task = task_service.create(TaskDraft(title="첨부 확인"))
    assert task.id is not None
    record_service = RecordService(InMemoryRecordRepository(), task_service)
    attachment_service = AttachmentService(
        InMemoryAttachmentRepository(),
        ManagedAttachmentStorage(tmp_path / "attachments"),
        task_service,
    )
    window = MainWindow(
        AppSettings(),
        task_service,
        record_service=record_service,
        attachment_service=attachment_service,
    )
    qtbot.addWidget(window)
    window._set_view(TaskView.ALL)
    window._task_list.setCurrentIndex(window._task_model.index_for_task(task.id))

    action_labels = [action.text() for action in window._build_task_context_menu(task).actions()]

    assert "첨부파일 보기 · 관리…" in action_labels
    assert "첨부파일 추가…" not in action_labels


def test_compact_window_uses_small_navigation_and_compact_filters(
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
    assert window._filter_options.isHidden()
    assert window._filter_toggle.isVisible()
    assert window._result_count.geometry().right() <= window._filter_bar.contentsRect().right()


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


def test_close_hides_to_tray_until_explicit_quit(
    qtbot: QtBot, task_service: TaskService
) -> None:
    shutdowns: list[bool] = []
    window = MainWindow(
        AppSettings(minimize_to_tray=True),
        task_service,
        on_shutdown=lambda: shutdowns.append(True),
    )
    qtbot.addWidget(window)
    tray = FakeTrayIcon()
    window._tray_icon = cast(Any, tray)
    window.show()

    window.close()

    assert window.isHidden()
    assert shutdowns == []
    assert tray.messages

    window._force_quit = True
    window.close()
    assert shutdowns == [True]


def test_external_quick_add_command_opens_all_view_input(
    qtbot: QtBot, task_service: TaskService
) -> None:
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.show()

    window.handle_external_command("quick-add")

    assert window._current_view is TaskView.ALL
    assert window.focusWidget() is window._quick_add_edit


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


def test_due_reminder_opens_in_app_alert_and_can_be_snoozed(qtbot: QtBot) -> None:
    task_repository = InMemoryTaskRepository()
    task_service = TaskService(task_repository)
    reminder_repository = InMemoryReminderRepository(task_repository)
    reminder_service = ReminderService(reminder_repository, task_service)
    now = datetime.now(UTC).replace(microsecond=0)
    task = task_service.create(
        TaskDraft(title="알림 테스트", starts_at=now - timedelta(minutes=1)),
        now=now - timedelta(minutes=2),
    )
    assert task.id is not None
    reminder_service.replace_rules(
        task.id,
        (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),),
    )
    window = MainWindow(AppSettings(), task_service, reminder_service=reminder_service)
    qtbot.addWidget(window)
    window.show()

    qtbot.waitUntil(lambda: window._reminder_dialog is not None, timeout=1_000)
    assert window._reminder_dialog is not None
    window._reminder_dialog.snooze_minutes.setValue(35)
    qtbot.mouseClick(window._reminder_dialog.snooze_button, Qt.MouseButton.LeftButton)

    delivery = next(iter(reminder_repository.deliveries.values()))
    assert delivery.status is ReminderDeliveryStatus.SNOOZED
    assert delivery.snoozed_until is not None
    assert delivery.snoozed_until == delivery.updated_at + timedelta(minutes=35)
    local_due = delivery.snoozed_until.astimezone(ZoneInfo("Asia/Seoul"))
    assert f"{local_due:%m월 %d일 %H:%M}" in window.statusBar().currentMessage()
    index = window._task_model.index_for_task(task.id)
    assert index.isValid()
    assert (
        window._task_model.data(index, TaskListModel.SNOOZED_UNTIL_ROLE)
        == delivery.snoozed_until
    )
    window._task_list.setCurrentIndex(index)
    assert f"다시 알림: {local_due:%Y.%m.%d %H:%M}" in window._detail_schedule.text()
    window._show_calendar()
    assert any(
        "재알림" in window._calendar_page.day_list.item(row).text()
        for row in range(window._calendar_page.day_list.count())
    )


def test_reminder_actions_immediately_refresh_current_view(qtbot: QtBot) -> None:
    task_repository = InMemoryTaskRepository()
    task_service = TaskService(task_repository)
    reminder_repository = InMemoryReminderRepository(task_repository)
    reminder_service = ReminderService(reminder_repository, task_service)
    now = datetime.now(UTC).replace(microsecond=0)
    task = task_service.create(
        TaskDraft(title="즉시 반영 업무", starts_at=now - timedelta(minutes=1)),
        now=now - timedelta(minutes=2),
    )
    assert task.id is not None
    reminder_service.replace_rules(
        task.id,
        (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),),
    )
    window = MainWindow(AppSettings(), task_service, reminder_service=reminder_service)
    qtbot.addWidget(window)
    window.show()

    qtbot.waitUntil(lambda: window._reminder_dialog is not None, timeout=1_000)
    overdue = window._task_model.entry_at(
        window._task_model.index_for_group(TaskGroup.OVERDUE)
    )
    assert isinstance(overdue, GroupHeader)
    assert overdue.total == 1
    assert window._reminder_dialog is not None
    qtbot.mouseClick(window._reminder_dialog.complete_button, Qt.MouseButton.LeftButton)

    assert not window._task_model.index_for_group(TaskGroup.OVERDUE).isValid()
    completed = window._task_model.entry_at(
        window._task_model.index_for_group(TaskGroup.COMPLETED)
    )
    assert isinstance(completed, GroupHeader)
    assert completed.total == 1


def test_acknowledging_reminder_refreshes_current_view(qtbot: QtBot, monkeypatch) -> None:
    task_repository = InMemoryTaskRepository()
    task_service = TaskService(task_repository)
    reminder_repository = InMemoryReminderRepository(task_repository)
    reminder_service = ReminderService(reminder_repository, task_service)
    now = datetime.now(UTC).replace(microsecond=0)
    task = task_service.create(
        TaskDraft(title="확인 후 갱신", starts_at=now - timedelta(minutes=1)),
        now=now - timedelta(minutes=2),
    )
    assert task.id is not None
    reminder_service.replace_rules(
        task.id,
        (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),),
    )
    window = MainWindow(AppSettings(), task_service, reminder_service=reminder_service)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window._reminder_dialog is not None, timeout=1_000)
    refreshes: list[bool] = []
    monkeypatch.setattr(window, "_refresh_tasks", lambda: refreshes.append(True))

    assert window._reminder_dialog is not None
    qtbot.mouseClick(window._reminder_dialog.acknowledge_button, Qt.MouseButton.LeftButton)

    assert refreshes == [True]


def test_hidden_app_shows_persistent_topmost_alert(
    qtbot: QtBot,
) -> None:
    task_repository = InMemoryTaskRepository()
    task_service = TaskService(task_repository)
    reminder_repository = InMemoryReminderRepository(task_repository)
    reminder_service = ReminderService(reminder_repository, task_service)
    now = datetime.now(UTC).replace(microsecond=0)
    task = task_service.create(
        TaskDraft(title="숨김 알림", starts_at=now - timedelta(minutes=1)),
        now=now - timedelta(minutes=2),
    )
    assert task.id is not None
    reminder_service.replace_rules(
        task.id,
        (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),),
    )
    window = MainWindow(AppSettings(), task_service, reminder_service=reminder_service)
    qtbot.addWidget(window)
    tray = FakeTrayIcon()
    window._tray_icon = cast(Any, tray)

    window._check_reminders()

    assert window._reminder_dialog is not None
    assert window._reminder_dialog.isVisible()
    assert window._reminder_dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert tray.messages


def test_today_view_has_collapsible_completed_section(
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

    window._on_list_clicked(window._task_model.index_for_group(TaskGroup.COMPLETED))

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
    assert pinned.text() == "✓고정"
    assert "필터 2개" in window._result_count.text()
    qtbot.mouseClick(clear, Qt.MouseButton.LeftButton)
    assert window._task_model.total_task_count == 2
    assert pinned.text() == "고정"


def test_today_filter_feedback_explains_hidden_all_day_task(
    qtbot: QtBot, tmp_path: Path
) -> None:
    repository = InMemoryTaskRepository()
    task_service = TaskService(repository, timezone="Asia/Seoul")
    zone = ZoneInfo("Asia/Seoul")
    today = datetime.now(zone).date()
    start = datetime.combine(today, time.min, tzinfo=zone).astimezone(UTC)
    task_service.create(
        TaskDraft(
            title="오늘 종일 일정",
            all_day=True,
            starts_at=start,
            ends_at=(datetime.combine(today, time.min, tzinfo=zone) + timedelta(days=1)).astimezone(
                UTC
            ),
        )
    )
    attachment_service = AttachmentService(
        InMemoryAttachmentRepository(),
        ManagedAttachmentStorage(tmp_path / "attachments"),
        task_service,
    )
    settings = AppSettings(
        view_preferences={
            TaskView.TODAY.value: {
                "pinned_only": True,
                "has_attachments": True,
            }
        }
    )
    window = MainWindow(
        settings,
        task_service,
        attachment_service=attachment_service,
    )
    qtbot.addWidget(window)
    window.show()

    assert window._task_model.total_task_count == 0
    assert window._pinned_filter.text() == "✓고정"
    assert window._attachment_filter.text() == "✓첨부"
    assert "필터 조건" in window._empty_description.text()

    qtbot.mouseClick(window._clear_filters_button, Qt.MouseButton.LeftButton)

    assert window._task_model.total_task_count == 1
    today_header = window._task_model.entry_at(
        window._task_model.index_for_group(TaskGroup.IN_PROGRESS)
    )
    assert isinstance(today_header, GroupHeader)
    assert today_header.label == "오늘 할 일"
    assert today_header.total == 1

    window._set_view(TaskView.UPCOMING)

    assert window._task_model.total_task_count == 0
    assert not window._task_model.index_for_group(TaskGroup.IN_PROGRESS).isValid()


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


def test_view_preferences_and_one_line_mode_are_saved(
    qtbot: QtBot, task_service: TaskService
) -> None:
    saved: list[AppSettings] = []
    window = MainWindow(AppSettings(), task_service, save_settings=saved.append)
    qtbot.addWidget(window)
    window._set_view(TaskView.ALL)
    status = window.findChild(QComboBox, "statusFilter")
    assert status is not None
    status.setCurrentIndex(status.findData(TaskStatus.ACTIVE.value))
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


def test_calendar_emphasizes_three_day_items_and_shows_scroll_cue(
    qtbot: QtBot, task_service: TaskService
) -> None:
    zone = ZoneInfo("Asia/Seoul")
    today = datetime.now(zone).date()
    start = datetime.combine(today, time.min, tzinfo=zone).astimezone(UTC)
    for index in range(3):
        task_service.create(
            TaskDraft(
                title=f"확인할 일정 {index + 1}",
                all_day=True,
                starts_at=start,
                ends_at=start + timedelta(days=1),
            )
        )
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.resize(760, 560)
    window.show()
    window._show_calendar()
    qtbot.wait(20)

    page = window._calendar_page
    assert page.day_list.count() == 3
    assert page.day_count.text() == "총 3개 일정"
    assert page.day_count.property("hasMany") is True
    assert page.day_more_hint.isVisible()
    assert "총 3개" in page.day_more_hint.text()
    assert (
        page.day_list.verticalScrollBarPolicy()
        is Qt.ScrollBarPolicy.ScrollBarAlwaysOn
    )
    assert page.day_list.verticalScrollBar().isVisible()


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


def test_dense_calendar_loads_full_selected_day_on_demand(
    qtbot: QtBot, task_service: TaskService
) -> None:
    zone = ZoneInfo("Asia/Seoul")
    today = datetime.now(zone).date()
    tomorrow = today + timedelta(days=1)
    today_start = datetime.combine(today, time.min, tzinfo=zone).astimezone(UTC)
    tomorrow_start = datetime.combine(tomorrow, time.min, tzinfo=zone).astimezone(UTC)
    task_service.create(
        TaskDraft(
            title="오늘 일정",
            all_day=True,
            starts_at=today_start,
            ends_at=today_start + timedelta(days=1),
        )
    )
    for index in range(121):
        task_service.create(
            TaskDraft(
                title=f"내일 밀집 일정 {index:03d}",
                all_day=True,
                starts_at=tomorrow_start,
                ends_at=tomorrow_start + timedelta(days=1),
            )
        )
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.resize(1280, 800)
    window.show()
    window._show_calendar()

    assert window._calendar_page.summary_label.isVisible()
    assert window._calendar_page.day_list.count() == 1
    calendar = window._calendar_page.calendar
    grid_start, _grid_end = calendar.visible_date_range
    offset = (tomorrow - grid_start).days
    row, column = divmod(offset, 7)
    row_height = (calendar.height() - calendar.HEADER_HEIGHT) / 6
    point = QPoint(
        int((column + 0.5) * calendar.width() / 7),
        int(calendar.HEADER_HEIGHT + (row + 0.4) * row_height),
    )

    qtbot.mouseClick(calendar, Qt.MouseButton.LeftButton, pos=point)

    assert window._calendar_page.day_list.count() == 121


def test_calendar_completes_only_selected_recurrence(
    qtbot: QtBot, task_service: TaskService
) -> None:
    zone = ZoneInfo("Asia/Seoul")
    today = datetime.now(zone).date()
    start = datetime.combine(today, time.min, tzinfo=zone).astimezone(UTC)
    task = task_service.create(
        TaskDraft(
            title="매일 반복 확인",
            all_day=True,
            starts_at=start,
            ends_at=start + timedelta(days=1),
            recurrence_rule="FREQ=DAILY;INTERVAL=1",
        )
    )
    assert task.id is not None
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    window.resize(1280, 800)
    window.show()
    window._show_calendar()
    window._calendar_page.day_list.setCurrentRow(0)

    qtbot.mouseClick(window._complete_button, Qt.MouseButton.LeftButton)

    item = window._calendar_page.day_list.item(0)
    scheduled = item.data(Qt.ItemDataRole.UserRole)
    assert isinstance(scheduled, ScheduledTask)
    assert scheduled.occurrence_status is OccurrenceStatus.COMPLETED
    assert task_service.get(task.id).status is TaskStatus.ACTIVE
