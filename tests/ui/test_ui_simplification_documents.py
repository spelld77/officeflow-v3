"""Compact catalog summaries do not conflate export checks and destructive selections."""

from threading import Event

from PySide6.QtWidgets import QPushButton

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.presentation.document_actions import file_actions
from tests.integration.test_document_lifecycle import lifecycle, original
from tests.ui.test_document_paging_delete import browser


def test_browser_keeps_counts_separate_and_full_information_available(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        work = facade.create(
            facade.catalog.get("attachment", item.id), DocumentOperation.WORK, filename="작업.txt"
        )
        facade.create(
            facade.catalog.get("attachment", work.attachment_id),
            DocumentOperation.VERSION,
            label="검토본",
            memo="긴 메모 " * 300,
        )
        box = browser(env, facade, item.task_id, tmp_path)
        qtbot.addWidget(box)
        box.kind.setCurrentIndex(0)
        box.show()
        box.list.setCurrentRow(0)
        qtbot.waitUntil(lambda: box._thread is None)
        box.check_page(True)
        assert "저장 체크 1개" in box.selection_status.full_text
        assert "출처 확인 중" not in box.summary.full_text
        assert "출처 이름 변경됨" not in box.summary.full_text
        assert box.summary.full_text.count("\n") <= 1
        assert not box.all_families.isVisible()
        names = [button.text() for button in box.findChildren(QPushButton) if button.isVisible()]
        assert names == ["열기", "체크한 파일 저장…", "더보기 ▾"]
        box.information()
        info = next(
            d
            for d in file_actions(box, facade).dialogs
            if d.objectName() == "documentInformationDialog"
        )
        qtbot.waitUntil(lambda: info._thread is None)
        assert "긴 메모 " * 300 in info.text.toPlainText()
        assert "생성 당시 출처" in info.text.toPlainText()
        box.list.clearSelection()
        assert not box.summary.isVisible() and box.save_button.isEnabled()
        box.clear_checks()
        assert not box.save_button.isEnabled()
        box.shutdown()


def test_late_file_information_cannot_overwrite_new_selection(qtbot, tmp_path, monkeypatch):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        work = facade.create(
            facade.catalog.get("attachment", item.id),
            DocumentOperation.WORK,
            filename="새 선택.txt",
        )
        box = browser(env, facade, item.task_id, tmp_path)
        qtbot.addWidget(box)
        entered, release = Event(), Event()
        get = facade.catalog.get

        def slow_get(kind, identity):
            if identity == item.id:
                entered.set()
                assert release.wait(5)
            return get(kind, identity)

        monkeypatch.setattr(facade.catalog, "get", slow_get)
        try:
            box.show()
            original_row = next(
                i
                for i in range(box.list.count())
                if box.list.item(i).data(256) == ("attachment", item.id)
            )
            box.list.setCurrentRow(original_row)
            qtbot.waitUntil(entered.is_set)
            box.list.setCurrentRow(1 - original_row)
            assert box.current().id == work.attachment_id
            release.set()
            qtbot.waitUntil(lambda: box._thread is None)
            assert box.summary.full_text.startswith("작업본")
            box.check_page(True)
            assert "출처 확인 중" not in box.summary.full_text
        finally:
            release.set()
            box.shutdown()


def test_multiple_highlights_disable_single_commands_not_delete(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        facade.create(
            facade.catalog.get("attachment", item.id), DocumentOperation.WORK, filename="작업.txt"
        )
        box = browser(env, facade, item.task_id, tmp_path)
        qtbot.addWidget(box)
        box.show()
        box.list.setCurrentRow(0)
        box.list.item(1).setSelected(True)
        menu = box._build_menu()
        actions = {a.text(): a for a in menu.actions()}
        assert not box.open_button.isEnabled()
        assert not actions["파일 정보…"].isEnabled()
        assert not actions["작업본 만들기…"].isEnabled()
        assert actions["선택 파일 2개 삭제…"].isEnabled()
        assert box.current() is None
        assert "선택 2개" in box.summary.full_text
        box.read_only = True
        assert not next(a for a in box._build_menu().actions() if "삭제" in a.text()).isEnabled()
        box.delete()
        assert not file_actions(box, facade).dialogs
        box.shutdown()
