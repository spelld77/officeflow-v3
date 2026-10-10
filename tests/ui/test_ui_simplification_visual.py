"""Small-window and injected dark-palette captures of U22's simplified surfaces."""

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QMenu, QPushButton

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.tasks import TaskDraft
from officeflow.presentation.document_actions import ViewCacheDialog, file_actions
from officeflow.presentation.task_history_dialog import TaskHistoryDialog
from officeflow.presentation.theme import LIGHT_STYLESHEET
from tests.integration.test_document_lifecycle import lifecycle, original
from tests.ui.test_attachment_feedback import make_records
from tests.ui.test_document_paging_delete import browser
from tests.ui.test_ui_simplification_reminders import editor_with_runtime


def capture(widget, label, suffix, *, minimum_viewport=0):
    if widget.layout() is not None:
        widget.layout().activate()
    if minimum_viewport:
        assert widget.list.viewport().height() >= minimum_viewport
    for button in widget.findChildren(QPushButton):
        if button.window() is not widget or not button.isVisible():
            continue
        assert widget.rect().contains(
            QRect(button.mapTo(widget, button.rect().topLeft()), button.size())
        ), button.text()
        assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 8
        assert button.height() >= button.fontMetrics().height() + 8, (
            button.text(),
            button.geometry(),
        )
    background = widget.palette().color(QPalette.ColorRole.Window)
    foreground = widget.palette().color(QPalette.ColorRole.WindowText)
    assert background.lightness() > 150 and foreground.lightness() < 100
    directory = Path("artifacts/ui-simplification-captures")
    directory.mkdir(parents=True, exist_ok=True)
    assert widget.grab().save(str(directory / f"{label}-{suffix}.png"))


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("small", [False, True])
def test_simplified_surfaces_keep_list_space_and_readable_actions(
    qtbot, qapp, tmp_path, dark, small
):
    palette, font = QPalette(qapp.palette()), QFont(qapp.font())
    windows = []
    suffix = f"{'dark' if dark else 'light'}-{'small' if small else 'normal'}-{os.environ.get('QT_SCALE_FACTOR', '1')}"
    try:
        if os.environ.get("OFFICEFLOW_RENDER_FONTS") == "1":
            for filename in ("malgun.ttf", "malgunbd.ttf", "segoeui.ttf"):
                assert (
                    QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / filename)) >= 0
                )
            qapp.setFont(QFont("Malgun Gothic", font.pointSize()))
        injected = QPalette(palette)
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            injected.setColor(role, QColor("#202020" if dark else "#f4f7fb"))
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
        ):
            injected.setColor(role, QColor("#eeeeee" if dark else "#172033"))
        qapp.setPalette(injected)
        with lifecycle(tmp_path) as (env, facade):
            try:
                item = original(env, facade)
                work = facade.create(
                    facade.catalog.get("attachment", item.id),
                    DocumentOperation.WORK,
                    filename="아주 긴 업무 파일 이름과 추가 설명 " * 4 + ".txt",
                )
                facade.create(
                    facade.catalog.get("attachment", work.attachment_id),
                    DocumentOperation.VERSION,
                    label="부서 검토 완료본",
                    memo="상세 메모 " * 300,
                )
                box = browser(env, facade, item.task_id, tmp_path)
                windows.append(box)
                qtbot.addWidget(box)
                box.resize(480, 420) if small else box.resize(640, 480)
                box.kind.setCurrentIndex(0)
                box.show()
                box.list.setCurrentRow(0)
                qtbot.waitUntil(lambda: box._thread is None)
                box.check_page(True)
                qtbot.wait(10)
                capture(box, "catalog", suffix, minimum_viewport=150)
                box.information()
                info = next(
                    d
                    for d in file_actions(box, facade).dialogs
                    if d.objectName() == "documentInformationDialog"
                )
                qtbot.waitUntil(lambda: info._thread is None)
                capture(info, "file-information", suffix)
                assert "상세 메모 " * 300 in info.text.toPlainText()
                info.reject()
                box.kind.setCurrentIndex(1)
                box.list.setCurrentRow(0)
                box.list.item(1).setSelected(True)
                capture(box, "catalog-multiple", suffix, minimum_viewport=150)
                menu = box._build_menu()
                menu.popup(box.more_button.mapToGlobal(box.more_button.rect().bottomLeft()))
                qtbot.wait(10)
                capture(menu, "catalog-menu", suffix)
                menu.close()
                menu.deleteLater()

                path = facade.prepare_open("attachment", item.id)
                path.write_bytes(b"saved edit")
                copy = facade.cache.by_path(path)
                facade.cache.confirm_closed(copy, copy.use_token)
                cache = ViewCacheDialog(facade)
                windows.append(cache)
                qtbot.addWidget(cache)
                cache.resize(480, 400) if small else cache.resize(600, 450)
                cache.show()
                cache.list.setCurrentRow(0)
                qtbot.wait(10)
                capture(cache, "cache", suffix, minimum_viewport=150)
                assert cache.preserve_button.isEnabled()
                assert not cache.navigation.isVisible()

                for index in range(5):
                    env.tasks.update(item.task_id, TaskDraft(f"변경 내용 {index}"))
                history = TaskHistoryDialog(
                    item.task_id, "아주 긴 업무 제목 " * 20, env.tasks.history, lifecycle=facade
                )
                windows.append(history)
                qtbot.addWidget(history)
                history.resize(420, 350) if small else history.resize(720, 520)
                history.show()
                qtbot.waitUntil(lambda: history._thread is None)
                capture(history, "history", suffix, minimum_viewport=120 if small else 150)
                history.list.setCurrentRow(0)
                history.begin_compare()
                history.list.item(1).setCheckState(Qt.CheckState.Checked)
                qtbot.wait(10)
                capture(history, "history-compare", suffix, minimum_viewport=60)
                history.date_filter.setChecked(True)
                qtbot.waitUntil(lambda: history._thread is None)
                qtbot.wait(10)
                capture(history, "history-period-compare", suffix, minimum_viewport=40)

                env.records.add_checklist_item(item.task_id, "체크리스트 항목")
                records = make_records(env, item, tmp_path)
                windows.append(records)
                qtbot.addWidget(records)
                records.show()
                records.tabs.setCurrentIndex(0)
                records.checklist_list.setCurrentRow(0)
                assert not any(
                    b.isVisible() and b.text() in {"위로", "아래로"}
                    for b in records.findChildren(QPushButton)
                )
                checklist_menu = records._build_checklist_menu()
                assert isinstance(checklist_menu, QMenu)
                checklist_menu.popup(
                    records.checklist_list.mapToGlobal(records.checklist_list.rect().center())
                )
                qtbot.wait(10)
                capture(checklist_menu, "checklist-menu", suffix)
                checklist_menu.close()
                checklist_menu.deleteLater()

                editor, _state = editor_with_runtime(qtbot)
                # Standalone fixture: reproduce the application's explicit light stylesheet.
                editor.setStyleSheet(LIGHT_STYLESHEET)
                windows.append(editor)
                editor.resize(460, 520)
                editor.show()
                editor._scroll.ensureWidgetVisible(editor.reminder_summary)
                qtbot.wait(10)
                capture(editor, "editor", suffix)
                editor.start_date_edit.setDate(editor.start_date_edit.date().addDays(1))
                qtbot.wait(10)
                editor._scroll.ensureWidgetVisible(editor.reminder_summary)
                qtbot.wait(10)
                assert "저장 전 변경 있음" in editor.reminder_summary.text()
                assert editor.reminder_summary.height() >= editor.reminder_summary.heightForWidth(
                    editor.reminder_summary.width()
                )
                capture(editor, "editor-draft", suffix)
                assert editor.reminder_summary.text().count("알림 1개") == 1
            finally:
                for window in reversed(windows):
                    if hasattr(window, "shutdown"):
                        window.shutdown()
                    window.close()
    finally:
        qapp.setPalette(palette)
        qapp.setFont(font)
