from datetime import UTC, datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette

from officeflow.application.tasks import TaskDraft
from officeflow.presentation.task_history_dialog import HistoryCompareDialog, TaskHistoryDialog


def test_history_pages_and_comparison_keep_list_height(qtbot, task_service):
    now = datetime.now(UTC)
    task = task_service.create(TaskDraft("test"), now=now)
    for index in range(53):
        task_service.update(task.id, TaskDraft(f"name {index}"), now=now)
    dialog = TaskHistoryDialog(task.id, task.title, task_service.history)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.list.count() == 50 and dialog._thread is None)
    assert dialog.next.isEnabled() and not dialog.previous.isEnabled()
    dialog.list.setCurrentRow(0)
    dialog.choose_compare()
    qtbot.mouseClick(dialog.next, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: dialog.list.count() == 4 and dialog._thread is None)
    dialog.list.setCurrentRow(0)
    dialog.choose_compare()
    dialog.open_compare()
    qtbot.waitUntil(lambda: bool(dialog._children))
    child = dialog._children[0]
    qtbot.waitUntil(lambda: child._thread is None and bool(child.after.toPlainText()))
    assert dialog.list.count() == 4 and len(dialog._compared) == 2
    dialog.reject()
    assert dialog._closed and child._closed


def test_light_palette_and_narrow_comparison(qtbot, task_service):
    task = task_service.create(TaskDraft("팔레트", description="예산 100만원"))
    task_service.update(task.id, TaskDraft("팔레트", description="예산 100(천원)"))
    row = task_service.history.page(task.id).items[0]
    entry = task_service.history.get(task.id, row.id)
    dialog = HistoryCompareDialog("비교", entry.before, entry.after, timezone="Asia/Seoul")
    qtbot.addWidget(dialog)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#202020"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#202020"))
    palette.setColor(QPalette.ColorRole.Text, QColor("black"))
    dialog.setPalette(palette)
    dialog.resize(450, 520)
    dialog.show()
    qtbot.waitUntil(lambda: dialog._thread is None and bool(dialog.after.toPlainText()))
    assert dialog.splitter.orientation() == Qt.Orientation.Vertical
    assert "100만원" in dialog.before.toPlainText()
    assert "100(천원)" in dialog.after.toPlainText()
    assert "#FFFFFF" in dialog.before.styleSheet()
    dialog.reject()


def test_committed_change_refreshes_first_page_only(qtbot, task_service):
    task = task_service.create(TaskDraft("before"))
    dialog = TaskHistoryDialog(task.id, task.title, task_service.history)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.list.count() == 1 and dialog._thread is None)
    task_service.update(task.id, TaskDraft("after"))
    qtbot.waitUntil(lambda: dialog.list.count() == 2 and dialog._thread is None)
    dialog.reject()


def test_ambiguous_editor_save_retry_reuses_receipt_and_does_not_create_twice(
    qtbot, task_service, monkeypatch
):
    from officeflow.application.tasks import TaskView
    from officeflow.infrastructure.database.task_mutations import TaskMutationRunner
    from officeflow.infrastructure.settings.store import AppSettings
    from officeflow.presentation.main_window import MainWindow
    from officeflow.presentation.task_editor import TaskEditorDialog

    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    editor = TaskEditorDialog(
        timezone="Asia/Seoul",
        on_save=lambda dialog: window._save_editor(dialog, None, ()),
        parent=window,
    )
    qtbot.addWidget(editor)
    editor.title_edit.setText("재시도 검증")
    original = TaskMutationRunner.execute
    failed = [False]

    def ambiguous(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if not failed[0]:
            failed[0] = True
            raise OSError("synthetic response lost after commit")
        return result

    monkeypatch.setattr(TaskMutationRunner, "execute", ambiguous)
    editor.show()
    editor._validate_and_accept()
    assert "response lost" in editor.error_label.text()
    assert editor.isVisible()
    editor._validate_and_accept()
    assert not editor.isVisible()
    assert len(task_service.list(TaskView.ALL)) == 1
    assert "이미 처리" in window.statusBar().currentMessage()


def test_retained_checklist_only_change_is_not_reported_as_no_change(task_service):
    task = task_service.create(TaskDraft("체크 상태"))
    values = task_service.history.current(task.id)
    left = {"checklist": [{"content": "검토", "is_done": False}], "files": []}
    right = {"checklist": [{"content": "검토", "is_done": True}], "files": []}
    before, after, note, _diff = HistoryCompareDialog._prepare(
        values, values, "Asia/Seoul", left, right
    )
    assert "업무 정보 변경: 없음" in note
    assert "보관 정보 변경: 체크리스트" in note
    assert "☐ 검토" in before and "☑ 검토" in after


def test_large_korean_comparison_keeps_complete_text_and_skips_highlight(qtbot, task_service):
    description = "장문의 업무 자료\n" * 20_000
    task = task_service.create(TaskDraft("긴 원문", description=description))
    left = task_service.history.current(task.id)
    right = dict(left, description=description + "마지막 수정")
    dialog = HistoryCompareDialog("긴 원문 비교", left, right, timezone="Asia/Seoul")
    qtbot.addWidget(dialog)
    dialog.resize(450, 520)
    dialog.show()
    qtbot.waitUntil(
        lambda: dialog._thread is None and bool(dialog.after.toPlainText()), timeout=10000
    )
    assert description in dialog.before.toPlainText()
    assert description + "마지막 수정" in dialog.after.toPlainText()
    assert "변경 강조 생략" in dialog.feedback.text()
    assert not dialog.before.extraSelections() and not dialog.after.extraSelections()
    dialog.reject()
