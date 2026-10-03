from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from shiboken6 import isValid

from officeflow.application.attachment_search import AttachmentSearchQuery
from officeflow.application.tasks import TaskDraft, TaskView
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow
from officeflow.presentation.record_dialog import TaskRecordsDialog
from tests.integration.test_attachment_search import search_environment


@pytest.fixture
def calendar_window(tmp_path, qtbot, monkeypatch):
    def unexpected_picker(*_args, **_kwargs):
        raise AssertionError("Opening attachments must not open a file picker")

    monkeypatch.setattr(QFileDialog, "getOpenFileNames", unexpected_picker)
    # Prime PySide's lazy Qt method bindings before replacing the static UI API.
    box = QMessageBox()
    box.deleteLater()
    monkeypatch.setattr(
        QMessageBox, "information", lambda *_args, **_kwargs: QMessageBox.StandardButton.Ok
    )
    zone = ZoneInfo("Asia/Seoul")
    today = datetime.now(zone).date()
    start = datetime.combine(today, time(9), tzinfo=zone).astimezone(UTC)
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="계약 검토", starts_at=start))
        env.records.add_checklist_item(task.id, "본문 확인")
        window = MainWindow(
            AppSettings(automatic_backup_enabled=False),
            env.tasks,
            record_service=env.records,
            attachment_service=env.attachments,
            attachment_search_service=env.search,
        )
        qtbot.addWidget(window)
        window.show()
        window._show_calendar()
        qtbot.wait(10)
        try:
            yield window, env, task, start
        finally:
            window.shutdown()
            window.close()
            qtbot.wait(10)


def _records(window):
    return [dialog for dialog in window._file_dialogs if isinstance(dialog, TaskRecordsDialog)]


def _patch_popup(window, monkeypatch, popup):
    # Patch the constructed instance: PySide's lazy method installation can
    # replace a class-level exec monkeypatch when its first QMenu is created.
    original = window._build_task_context_menu

    def build(task, **kwargs):
        menu = original(task, **kwargs)
        menu.exec = lambda position: popup(menu, position)
        return menu

    monkeypatch.setattr(window, "_build_task_context_menu", build)


@pytest.mark.parametrize("surface", ["bar", "day-list"])
def test_single_click_only_selects_and_double_click_opens_records(
    calendar_window, qtbot, monkeypatch, surface
):
    window, _env, task, _start = calendar_window
    monkeypatch.setattr(
        window, "_open_editor", lambda *_args, **_kwargs: pytest.fail("must not edit")
    )
    page = window._calendar_page
    selected_date = page.selected_date
    if surface == "bar":
        page.calendar.grab()
        hit = next(rect for rect, scheduled in page.calendar._task_hits if scheduled.id == task.id)
        target, position = page.calendar, hit.center().toPoint()
    else:
        target = page.day_list.viewport()
        position = page.day_list.visualItemRect(page.day_list.item(0)).center()
    qtbot.mouseClick(target, Qt.MouseButton.LeftButton, pos=position)
    assert not _records(window)
    assert page.records_button.isEnabled() and page.attachments_button.isEnabled()
    qtbot.mouseDClick(target, Qt.MouseButton.LeftButton, pos=position)
    qtbot.waitUntil(lambda: len(_records(window)) == 1)
    dialog = _records(window)[0]
    assert dialog.current_tab_key == "checklist" and dialog.checklist_list.count() == 1
    assert not dialog.isModal() and QApplication.activeModalWidget() is None
    assert page.isVisible() and page.selected_date == selected_date


@pytest.mark.parametrize("with_file", [False, True])
def test_attachment_button_shows_registered_files_or_empty_state(calendar_window, qtbot, with_file):
    window, env, task, _start = calendar_window
    if with_file:
        env.add(task.id, "계약서.txt")
        window._refresh_calendar()
    page = window._calendar_page
    page.day_list.setCurrentRow(0)
    assert ("첨부" in page.day_list.item(0).text()) is with_file
    page.attachments_button.click()
    dialog = _records(window)[0]
    assert dialog.current_tab_key == "attachments"
    assert dialog.attachment_list.count() == int(with_file)
    if with_file:
        assert "계약서.txt" in dialog.attachment_list.item(0).text()
    else:
        assert dialog.attachment_detail.text() == "첨부파일이 없습니다."
        assert dialog.add_attachment_button.isEnabled()


def test_buttons_reuse_window_without_overwriting_unsaved_input(calendar_window):
    window, _env, task, _start = calendar_window
    page = window._calendar_page
    page.day_list.setCurrentRow(0)
    page.records_button.click()
    dialog = _records(window)[0]
    dialog.checklist_edit.setText("아직 추가하지 않은 항목")
    dialog.result_edit.setPlainText("저장하지 않은 완료 요약")
    dialog.log_content_edit.setPlainText("저장하지 않은 업무일지")
    page.attachments_button.click()
    assert _records(window) == [dialog] and dialog.current_tab_key == "attachments"
    page.records_button.click()
    assert _records(window) == [dialog] and dialog.current_tab_key == "attachments"
    assert dialog.checklist_edit.text() == "아직 추가하지 않은 항목"
    assert dialog.result_edit.toPlainText() == "저장하지 않은 완료 요약"
    assert dialog.log_content_edit.toPlainText() == "저장하지 않은 업무일지"
    assert window._selected_task_id == task.id


def test_recent_tab_is_remembered_for_new_window_but_not_new_application(calendar_window, qtbot):
    window, env, _task, _start = calendar_window
    page = window._calendar_page
    page.day_list.setCurrentRow(0)
    page.records_button.click()
    dialog = _records(window)[0]
    dialog.select_tab("work_log")
    dialog.reject()
    qtbot.waitUntil(lambda: not isValid(dialog))
    page.records_button.click()
    assert _records(window)[0].current_tab_key == "work_log"
    other = MainWindow(
        AppSettings(automatic_backup_enabled=False),
        env.tasks,
        record_service=env.records,
        attachment_service=env.attachments,
    )
    qtbot.addWidget(other)
    try:
        other._show_calendar()
        other._calendar_page.day_list.setCurrentRow(0)
        other._calendar_page.records_button.click()
        assert _records(other)[0].current_tab_key == "checklist"
    finally:
        other.shutdown()


@pytest.mark.parametrize("source", ["more-button", "right-bar", "right-list"])
def test_calendar_menu_prioritizes_records_and_routes_directly_to_work_log(
    calendar_window, qtbot, monkeypatch, source
):
    window, _env, task, _start = calendar_window
    page = window._calendar_page
    observed = []

    def popup(menu, _position):
        labels = [action.text() for action in menu.actions() if not action.isSeparator()]
        observed.append(labels)
        assert labels[:4] == [
            "업무 기록 열기",
            "체크리스트 보기",
            "업무일지 보기 · 작성",
            "첨부파일 보기 · 관리…",
        ]
        assert labels.count("첨부파일 보기 · 관리…") == 1
        assert "업무 수정" in labels and "바로 완료" in labels
        next(
            action for action in menu.actions() if action.text() == "업무일지 보기 · 작성"
        ).trigger()
        return None

    _patch_popup(window, monkeypatch, popup)
    if source == "more-button":
        page.day_list.setCurrentRow(0)
        page.more_button.click()
    elif source == "right-list":
        page._show_day_item_context_menu(
            page.day_list.visualItemRect(page.day_list.item(0)).center()
        )
    else:
        page.calendar.grab()
        rect = next(rect for rect, scheduled in page.calendar._task_hits if scheduled.id == task.id)
        qtbot.mouseClick(page.calendar, Qt.MouseButton.RightButton, pos=rect.center().toPoint())
    assert observed and _records(window)[0].current_tab_key == "work_log"


def test_calendar_menu_can_still_edit_template(calendar_window, monkeypatch):
    window, _env, task, _start = calendar_window
    captured = []
    monkeypatch.setattr(window, "_open_editor", lambda current, **_kwargs: captured.append(current))

    def popup(menu, _position):
        next(action for action in menu.actions() if action.text() == "업무 수정").trigger()

    _patch_popup(window, monkeypatch, popup)
    page = window._calendar_page
    page.day_list.setCurrentRow(0)
    page.more_button.click()
    assert [current.id for current in captured] == [task.id]
    assert not _records(window)


def test_empty_date_double_click_still_creates_schedule(calendar_window, qtbot, monkeypatch):
    window, _env, _task, _start = calendar_window
    captured = []
    monkeypatch.setattr(
        window, "_open_editor", lambda current, **kwargs: captured.append((current, kwargs))
    )
    calendar = window._calendar_page.calendar
    calendar.grab()
    position = QPoint(10, calendar.height() - 10)
    selected_date = calendar._date_at(position)
    qtbot.mouseDClick(calendar, Qt.MouseButton.LeftButton, pos=position)
    assert captured == [(None, {"initial_date": selected_date})]
    assert not _records(window)


def test_day_change_clears_selection_and_disables_actions(calendar_window):
    window, _env, _task, _start = calendar_window
    page = window._calendar_page
    page.day_list.setCurrentRow(0)
    assert page.records_button.isEnabled()
    page._select_date(page.selected_date + timedelta(days=1))
    assert window._selected_task_id is None and page._selected_task is None
    assert not any(
        button.isEnabled()
        for button in (page.records_button, page.attachments_button, page.more_button)
    )


def test_enter_shortcut_opens_calendar_records_not_editor(calendar_window, monkeypatch):
    window, _env, _task, _start = calendar_window
    monkeypatch.setattr(
        window, "_open_editor", lambda *_args, **_kwargs: pytest.fail("must not edit")
    )
    window._calendar_page.day_list.setCurrentRow(0)
    window._open_task_shortcut.activated.emit()
    assert len(_records(window)) == 1


def test_window_keyboard_enter_opens_calendar_records(calendar_window, qtbot, monkeypatch):
    window, _env, _task, _start = calendar_window
    monkeypatch.setattr(
        window, "_open_editor", lambda *_args, **_kwargs: pytest.fail("must not edit")
    )
    window._calendar_page.day_list.setCurrentRow(0)
    window.raise_()
    window.activateWindow()
    qtbot.waitActive(window)
    window._calendar_page.day_list.setFocus()
    QTest.keyClick(window.windowHandle(), Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: len(_records(window)) == 1)


def test_record_save_refreshes_calendar_without_losing_selection(calendar_window):
    window, env, task, _start = calendar_window
    page = window._calendar_page
    page.day_list.setCurrentRow(0)
    page.records_button.click()
    dialog = _records(window)[0]
    dialog.result_edit.setPlainText("완료 요약 저장")
    dialog._save_result()
    assert env.tasks.result_note(task.id) == "완료 요약 저장"
    assert page._selected_task.id == task.id and page.records_button.isEnabled()
    page.records_button.click()
    assert _records(window) == [dialog]


def test_failed_day_load_clears_stale_calendar_selection(calendar_window, monkeypatch):
    window, _env, _task, _start = calendar_window
    page = window._calendar_page
    page.day_list.setCurrentRow(0)

    def failed(*_args, **_kwargs):
        raise RuntimeError("조회 실패")

    monkeypatch.setattr(window._task_service, "calendar_schedule", failed)
    assert not window._refresh_calendar_day(page.selected_date)
    assert window._selected_task_id is None and page.day_list.count() == 0
    assert not page.records_button.isEnabled()


def test_recurring_records_use_clicked_occurrence_and_reuse_only_same_occurrence(calendar_window):
    window, env, _task, start = calendar_window
    recurring = env.tasks.create(
        TaskDraft(
            title="매일 점검", starts_at=start - timedelta(days=2), recurrence_rule="FREQ=DAILY"
        )
    )
    env.tasks.update_result_note(recurring.id, "오늘 회차 요약", occurrence_start=start)
    window._refresh_calendar()
    page = window._calendar_page
    occurrence = next(scheduled for scheduled in page._day_tasks if scheduled.id == recurring.id)
    window._open_calendar_task(occurrence)
    today_dialog = _records(window)[0]
    assert today_dialog._occurrence_start == start
    assert today_dialog.result_edit.toPlainText() == "오늘 회차 요약"
    today_dialog.result_edit.setPlainText("오늘 작성 중")
    window._open_calendar_task(occurrence)
    assert _records(window) == [today_dialog]
    page._select_date(page.selected_date + timedelta(days=1))
    tomorrow = next(scheduled for scheduled in page._day_tasks if scheduled.id == recurring.id)
    # Deliberately leave a stale main selection: activation must use its payload.
    window._selected_occurrence_start = start
    window._open_calendar_task(tomorrow)
    tomorrow_dialog = _records(window)[1]
    assert tomorrow_dialog._occurrence_start == start + timedelta(days=1)
    assert tomorrow_dialog.result_edit.toPlainText() == ""
    assert today_dialog.result_edit.toPlainText() == "오늘 작성 중"


def test_refresh_retains_exact_occurrence_when_multiple_spans_overlap(calendar_window):
    window, env, _task, start = calendar_window
    recurring = env.tasks.create(
        TaskDraft(
            title="겹치는 회차",
            starts_at=start - timedelta(days=2),
            ends_at=start + timedelta(days=1),
            recurrence_rule="FREQ=DAILY",
        )
    )
    window._refresh_calendar()
    page = window._calendar_page
    row = next(
        index
        for index in range(page.day_list.count())
        if page.day_list.item(index).data(Qt.ItemDataRole.UserRole).id == recurring.id
    )
    page.day_list.setCurrentRow(row)
    selected = page._selected_task.occurrence_start
    window._refresh_calendar()
    assert page._selected_task.id == recurring.id
    assert page._selected_task.occurrence_start == selected == window._selected_occurrence_start
    page.records_button.click()
    assert _records(window)[0]._occurrence_start == selected


def test_file_search_and_calendar_reuse_same_main_owned_record_window(calendar_window):
    window, env, task, _start = calendar_window
    env.add(task.id, "기록공유.txt")
    window._refresh_calendar()
    window._calendar_page.day_list.setCurrentRow(0)
    window._calendar_page.records_button.click()
    dialog = _records(window)[0]
    dialog.result_edit.setPlainText("입력 유지")
    hit = env.search.search_page(AttachmentSearchQuery(search="기록공유")).items[0]
    window._open_file_records(hit, True)
    assert _records(window) == [dialog] and dialog.current_tab_key == "attachments"
    assert dialog._selected_attachment_id == hit.attachment_id
    window._set_view(TaskView.ALL)
    window._select_task_for_action(env.tasks.get(task.id))
    window._open_selected_records()
    assert _records(window) == [dialog] and dialog.result_edit.toPlainText() == "입력 유지"


@pytest.mark.parametrize("size", [(760, 560), (1100, 760)])
def test_calendar_actions_and_three_rows_fit_without_clipping(
    calendar_window, qtbot, tmp_path, size
):
    window, env, _task, start = calendar_window
    for index in range(5):
        env.tasks.create(
            TaskDraft(title=f"추가 일정 {index}", starts_at=start + timedelta(minutes=index + 1))
        )
    window.resize(*size)
    window._refresh_calendar()
    page = window._calendar_page
    page.day_list.setCurrentRow(0)
    qtbot.wait(100)
    assert window.grab().save(str(tmp_path / "calendar-layout.png"))
    print(
        f"calendar row={page.day_list.sizeHintForRow(0)}, font={page.day_list.fontMetrics().height()}, "
        f"viewport={page.day_list.viewport().height()}, ratio={window.devicePixelRatioF()}"
    )
    print(
        f"calendar={page.calendar.geometry()}, panel={page.day_panel.geometry()}, "
        f"title={page.day_label.geometry()}, records={page.records_button.geometry()}, list={page.day_list.geometry()}"
    )
    assert (window.width(), window.height()) == size
    assert page.calendar.geometry().bottom() < page.day_panel.geometry().top()
    assert not window._edit_button.isVisible()
    assert window._detail_records_button.property("primaryAction") is True
    for button in (
        page.expand_list_button,
        page.records_button,
        page.attachments_button,
        page.more_button,
    ):
        assert button.isVisible()
        assert button.geometry().right() < page.day_panel.width()
        assert page.day_panel.rect().contains(button.geometry())
        assert button.width() >= button.sizeHint().width()
    assert (
        page.day_list.visualItemRect(page.day_list.item(2)).bottom()
        <= page.day_list.viewport().height()
    )
    assert page.day_more_hint.isVisible() and "3개 더 있음" in page.day_more_hint.text()
    if size[0] >= window.DETAIL_BREAKPOINT:
        window._set_view(TaskView.ALL)
        assert window._edit_button.isVisible()
        assert window._detail_records_button.property("primaryAction") is False
