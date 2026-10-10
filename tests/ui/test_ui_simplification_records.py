"""Short labels and rare checklist commands retain their real targets and protections."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QPushButton

from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow
from tests.integration.test_document_lifecycle import lifecycle, original
from tests.ui.test_attachment_feedback import make_records


def test_task_actions_distinguish_view_from_edit(qtbot, task_service):
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    assert window._open_selected_button.text() == "수정"
    assert window._open_selected_records_button.text() == "업무 보기"
    assert window._detail_records_button.text() == "업무 보기"
    assert window._detail_attachment_button.text() == "첨부"


def test_checklist_order_is_in_menu_and_keeps_selected_identity(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, _facade):
        item = original(env, _facade)
        first = env.records.add_checklist_item(item.task_id, "첫 항목")
        second = env.records.add_checklist_item(item.task_id, "둘째 항목")
        dialog = make_records(env, item, tmp_path)
        qtbot.addWidget(dialog)
        dialog.show()
        tab = dialog.tabs.widget(0)
        assert not any(b.text() in {"위로", "아래로"} for b in tab.findChildren(QPushButton))
        dialog.checklist_list.setCurrentRow(0)
        menu = dialog._build_checklist_menu()
        actions = {a.text(): a for a in menu.actions()}
        assert not actions["위로 이동"].isEnabled()
        assert actions["아래로 이동"].isEnabled()
        actions["아래로 이동"].trigger()
        assert [c.id for c in env.records.checklist_for_task(item.task_id)] == [second.id, first.id]
        assert dialog.checklist_list.currentItem().data(Qt.ItemDataRole.UserRole) == first.id
        dialog.shutdown()


def test_read_only_checklist_rejects_menu_and_direct_mutation(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        first = env.records.add_checklist_item(item.task_id, "첫 항목")
        second = env.records.add_checklist_item(item.task_id, "둘째 항목")
        dialog = make_records(env, item, tmp_path)
        qtbot.addWidget(dialog)
        dialog._read_only = True
        dialog._apply_read_only_mode()
        dialog._refresh_checklist()
        assert not dialog.checklist_list.item(0).flags() & Qt.ItemFlag.ItemIsEditable
        dialog.checklist_list.setCurrentRow(0)
        assert all(
            not a.isEnabled()
            for a in dialog._build_checklist_menu().actions()
            if not a.isSeparator()
        )
        dialog._move_checklist(1)
        dialog._delete_checklist()
        dialog.checklist_edit.setText("허용 안 됨")
        dialog._add_checklist()
        assert [c.id for c in env.records.checklist_for_task(item.task_id)] == [first.id, second.id]
        dialog.shutdown()


def test_failed_checklist_move_keeps_order_and_selection(qtbot, tmp_path, monkeypatch):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        first = env.records.add_checklist_item(item.task_id, "첫 항목")
        second = env.records.add_checklist_item(item.task_id, "둘째 항목")
        dialog = make_records(env, item, tmp_path)
        qtbot.addWidget(dialog)
        dialog.checklist_list.setCurrentRow(0)
        notices = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *args: notices.append(args))

        def fail(*args):
            raise RuntimeError("저장할 수 없음")

        monkeypatch.setattr(env.records, "reorder_checklist", fail)
        dialog._move_checklist(1)
        assert [c.id for c in env.records.checklist_for_task(item.task_id)] == [first.id, second.id]
        assert dialog.checklist_list.currentItem().data(Qt.ItemDataRole.UserRole) == first.id
        assert notices and "저장할 수 없음" in notices[0][2]
        dialog.shutdown()
