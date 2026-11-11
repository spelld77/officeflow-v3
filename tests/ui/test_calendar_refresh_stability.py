from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from PySide6.QtCore import Qt

from officeflow.application.reminder_preview import ReminderPreviewItem, ReminderPreviewResult
from officeflow.presentation.month_calendar import CalendarPage
from tests.unit.test_calendar_attention import scheduled


@pytest.fixture
def page(qtbot):
    page = CalendarPage(timezone="UTC")
    qtbot.addWidget(page)
    page._set_month_and_date(date(2026, 10, 14))
    page.set_day_tasks((scheduled(1, 14), scheduled(2, 14)))
    page.day_list.setCurrentRow(0)
    return page


def test_unchanged_poll_preserves_rows_selection_and_signals(page):
    rows = [page.day_list.item(i) for i in range(2)]
    selected, cleared, resets = [], [], []
    page.taskSelected.connect(selected.append)
    page.taskSelectionCleared.connect(lambda: cleared.append(True))
    page.day_list.model().modelReset.connect(lambda: resets.append(True))
    for _ in range(200):
        page.set_snoozed_reminders({})
    assert [page.day_list.item(i) for i in range(2)] == rows
    assert page.day_list.currentItem() is rows[0]
    assert not selected and not cleared and not resets


def test_snooze_changes_only_content_and_copies_input(page):
    row = page.day_list.currentItem()
    selected = []
    page.taskSelected.connect(selected.append)
    values = {(1, None): datetime(2026, 10, 14, 10, tzinfo=UTC)}
    page.set_snoozed_reminders(values)
    assert page.day_list.currentItem() is row
    assert "재알림" in row.text()
    values.clear()
    assert page._snoozed_until
    page.set_snoozed_reminders(values)
    assert "재알림" not in row.text()
    assert not selected


def test_same_text_new_action_payload_reuses_row(page):
    row = page.day_list.currentItem()
    old = row.data(Qt.ItemDataRole.UserRole)
    new = replace(old, task=replace(old.task, description="Updated description"))
    selected = []
    page.taskSelected.connect(selected.append)
    page.set_day_tasks((new, page._day_tasks[1]))
    assert page.day_list.currentItem() is row
    assert row.data(Qt.ItemDataRole.UserRole) == new
    assert page._selected_task == new
    assert selected == [new]


def test_explicit_calendar_click_overrides_old_selection_once(page):
    target = page._day_tasks[1]
    selected = []
    page.taskSelected.connect(selected.append)
    page._select_task(target)
    assert page.day_list.currentItem().data(Qt.ItemDataRole.UserRole) == target
    assert selected == [target]


@pytest.mark.parametrize("final_empty", [False, True])
def test_intermediate_refresh_keeps_intent_until_final_day_result(page, final_empty):
    tasks = page._day_tasks
    # set_overview performs this transition before requesting final day rows.
    from officeflow.application.tasks import CalendarOverview

    page.set_overview(CalendarOverview((), (), 0, False))
    page.set_snoozed_reminders({})
    page.set_reminder_preview(ReminderPreviewResult((), True))
    assert page._selection_intent == (1, None)
    assert not page.records_button.isEnabled()
    cleared = []
    page.taskSelectionCleared.connect(lambda: cleared.append(True))
    page.set_day_tasks(() if final_empty else tasks)
    if final_empty:
        assert page._selected_task is None
        assert page._selection_intent is None
        assert cleared == [True]
    else:
        assert page.day_list.currentItem().data(Qt.ItemDataRole.UserRole).id == 1


def test_group_transition_keeps_current_row(page):
    row = page.day_list.currentItem()
    task = page._day_tasks[0]
    alarm = ReminderPreviewItem(task.task, 1, None, task.starts_at, task.starts_at, "예정")
    page.set_reminder_preview(ReminderPreviewResult((alarm,), True))
    assert page.day_list.currentItem() is row
    assert page.reminder_details_button.isEnabled()
    page.set_reminder_preview(ReminderPreviewResult((), True))
    assert page.day_list.currentItem() is row
    assert not page.reminder_details_button.isEnabled()


def test_deleted_selection_is_cleared_without_selecting_first(page):
    page.set_day_tasks((page._day_tasks[1],))
    assert page.day_list.currentItem() is None
    assert page._selected_task is None


def test_reentrant_update_uses_latest_payload(page, qtbot):
    first = page._day_tasks[0]
    second = page._day_tasks[1]
    newer = replace(first, task=replace(first.task, description="Latest"))
    intermediate = replace(first, task=replace(first.task, description="Intermediate"))

    def update(task):
        if task == intermediate:
            page.set_day_tasks((newer, second))

    page.taskSelected.connect(update)
    page.set_day_tasks((intermediate, second))
    qtbot.waitUntil(lambda: page._selected_task == newer, timeout=1000)
    assert page.day_list.currentItem().data(Qt.ItemDataRole.UserRole) == newer


def test_failure_invalidates_cache_and_same_input_can_retry(page, monkeypatch):
    original = page._day_item_text

    def fail(task):
        raise ValueError("synthetic rendering failure")

    monkeypatch.setattr(page, "_day_item_text", fail)
    with pytest.raises(ValueError):
        page.set_snoozed_reminders({})
    assert page._last_refresh is None
    monkeypatch.setattr(page, "_day_item_text", original)
    page.set_snoozed_reminders({})
    assert page._last_refresh is not None


def test_failed_explicit_selection_retries_without_automatic_loop(page, monkeypatch):
    original = page._update_action_buttons
    target = page._day_tasks[1]

    def fail():
        page._refresh_day_list()
        raise ValueError("synthetic failure after row selection")

    monkeypatch.setattr(page, "_update_action_buttons", fail)
    with pytest.raises(ValueError):
        page._select_task(target)
    assert not page._refresh_timer.isActive()
    assert page._requested_selection == (target.id, None)
    monkeypatch.setattr(page, "_update_action_buttons", original)
    page.set_snoozed_reminders({})
    assert page._requested_selection is None
    assert page._selected_task == target


def test_shutdown_cancels_pending_refresh(page, qtbot):
    page._refresh_timer.start(0)
    page.shutdown()
    assert not page._refresh_timer.isActive()
    previous = page._last_refresh
    page.set_snoozed_reminders({})
    qtbot.wait(1)
    assert page._last_refresh == previous


def test_local_midnight_updates_identical_snooze_input(page, monkeypatch):
    import officeflow.presentation.task_list as task_list

    class Clock(datetime):
        day = 14

        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, cls.day, tzinfo=UTC)

    monkeypatch.setattr(task_list, "datetime", Clock)
    values = {(1, None): datetime(2026, 10, 14, 10, tzinfo=UTC)}
    page.set_snoozed_reminders(values)
    row = page.day_list.currentItem()
    before = row.text()
    Clock.day = 15
    page.set_snoozed_reminders(values)
    assert row.text() != before
    assert page.day_list.currentItem() is row


def test_structural_change_preserves_anchor_and_selection(page, qtbot):
    from PySide6.QtCore import QPoint

    page.resize(650, 600)
    page.show()
    tasks = tuple(scheduled(i, 14) for i in range(1, 31))
    page.set_day_tasks(tasks)
    page.day_list.setCurrentRow(15)
    page.day_list.scrollToItem(page.day_list.item(15))
    qtbot.wait(1)
    anchor = page.day_list.itemAt(QPoint(2, 2))
    key = page._row_key(anchor.data(Qt.ItemDataRole.UserRole))
    offset = page.day_list.visualItemRect(anchor).top()
    page.set_day_tasks((scheduled(0, 14), *tasks))
    anchor = next(
        page.day_list.item(i)
        for i in range(page.day_list.count())
        if page._row_key(page.day_list.item(i).data(Qt.ItemDataRole.UserRole)) == key
    )
    assert page.day_list.visualItemRect(anchor).top() == offset
    assert page.day_list.currentItem().data(Qt.ItemDataRole.UserRole).id == 16


def test_mode_return_does_not_restore_old_month_selection(page):
    page.set_mode("day")
    page.set_mode("month")
    page.set_day_tasks(page._day_tasks)
    assert page.day_list.currentItem() is None
