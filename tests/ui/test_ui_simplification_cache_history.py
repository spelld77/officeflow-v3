"""Cache paging stays bounded; history compare choices remain visible across pages."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QPushButton

from officeflow.application.tasks import TaskDraft
from officeflow.presentation.document_actions import ViewCacheDialog, file_actions
from officeflow.presentation.task_history_dialog import TaskHistoryDialog
from tests.integration.test_document_lifecycle import lifecycle, original


@pytest.mark.parametrize("count", [1, 100, 101])
def test_cache_page_has_exact_boundary_and_bounded_metadata_reads(
    qtbot, tmp_path, monkeypatch, count
):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        path = facade.prepare_open("attachment", item.id)
        base = facade.cache.by_path(path)
        for _index in range(count - 1):
            identity = uuid4().hex
            relative = f"cache/attachment-views/{base.generation}/{identity}/{base.name}"
            (facade.cache.root / relative).parent.mkdir(parents=True)
            facade.cache._save(replace(base, cache_id=identity, relative_path=relative))
        get = facade.cache.get
        calls = []

        def observe_metadata(*args):
            calls.append(args)
            return get(*args)

        monkeypatch.setattr(facade.cache, "get", observe_metadata)
        box = ViewCacheDialog(facade)
        qtbot.addWidget(box)
        box.show()
        assert len(calls) == min(count, 100)
        assert box.navigation.isVisible() == (count > 100)
        assert box.list.count() == min(count, 100)
        if count > 100:
            box.next()
            assert box.list.count() == 1 and not box.next_button.isEnabled()
        else:
            box.next()
            assert box.offset == 0
        box.shutdown()


def test_cache_multi_selection_needs_explicit_close_confirmation(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        for _index in range(2):
            item = original(env, facade)
            facade.prepare_open("attachment", item.id)
        box = ViewCacheDialog(facade)
        qtbot.addWidget(box)
        box.show()
        box.list.setCurrentRow(0)
        box.list.item(1).setSelected(True)
        assert box.current() is None and len(box._selected()) == 2
        actions = {a.text(): a for a in box._build_menu().actions()}
        assert not actions["선택 사본 버리기…"].isEnabled()
        assert actions["문서 닫음 확인…"].isEnabled()
        box.confirm_selected()
        controller = file_actions(box, facade)
        confirmation = next(
            d for d in controller.dialogs if d.objectName() == "viewClosedConfirmation"
        )
        assert not any(copy.closed for copy in facade.cache.page())
        confirmation.findChild(QCheckBox).setChecked(True)
        next(b for b in confirmation.findChildren(QPushButton) if b.text() == "확인").click()
        qtbot.waitUntil(lambda: controller._job_thread is None)
        assert all(copy.closed for copy in facade.cache.page())
        assert len(box._selected()) == 2
        box.shutdown()


def test_history_compare_checks_have_visible_names_and_survive_refresh(qtbot, task_service):
    task = task_service.create(TaskDraft("처음"))
    for index in range(52):
        task_service.update(task.id, TaskDraft(f"변경 {index}"))
    box = TaskHistoryDialog(task.id, task.title, task_service.history)
    qtbot.addWidget(box)
    box.show()
    qtbot.waitUntil(lambda: box._thread is None and box.list.count() == 50)
    assert not box.compare_panel.isVisible()
    box.list.setCurrentRow(0)
    box.begin_compare()
    assert len(box._compared) == 1 and box.compare_panel.isVisible()
    assert "제목" in box.compare_labels[0].full_text
    box.next_page()
    qtbot.waitUntil(lambda: box._thread is None and box.list.count() == 3)
    assert "현재 화면 밖" in box.compare_labels[0].full_text
    box.list.item(0).setCheckState(Qt.CheckState.Checked)
    assert len(box._compared) == 2 and box.compare.isEnabled()
    box.list.item(1).setCheckState(Qt.CheckState.Checked)
    assert len(box._compared) == 2
    assert box.list.item(1).checkState() == Qt.CheckState.Unchecked
    box.reset_page()
    qtbot.waitUntil(lambda: box._thread is None and box.list.count() == 50)
    assert len(box._compared) == 2 and box.compare.isEnabled()
    box.reset_compare()
    assert not box._compared and not box.compare_panel.isVisible()
    assert all(
        box.list.item(i).data(Qt.ItemDataRole.CheckStateRole) is None
        for i in range(box.list.count())
    )
    box.reject()


def test_history_rejects_different_scope_without_losing_first_choice(qtbot, task_service):
    task = task_service.create(TaskDraft("처음"))
    task_service.update(task.id, TaskDraft("바뀜"))
    box = TaskHistoryDialog(task.id, task.title, task_service.history)
    qtbot.addWidget(box)
    box.show()
    qtbot.waitUntil(lambda: box._thread is None)
    box.list.setCurrentRow(0)
    box.begin_compare()
    first = tuple(box._compared)
    other = tuple(box.list.item(1).data(Qt.ItemDataRole.UserRole))
    box._visible_rows[other] = replace(
        box._visible_rows[other],
        scope_key="other",
        occurrence_start=datetime(2026, 10, 1, tzinfo=UTC),
    )
    box.list.item(1).setCheckState(Qt.CheckState.Checked)
    assert tuple(box._compared) == first and not box.compare.isEnabled()
    assert "같은 회차" in box.feedback.text()
    assert box.list.item(1).checkState() == Qt.CheckState.Unchecked
    box.reject()


def test_stale_close_confirmation_reports_partial_failure(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        items = [original(env, facade) for _index in range(2)]
        paths = [facade.prepare_open("attachment", item.id) for item in items]
        box = ViewCacheDialog(facade)
        qtbot.addWidget(box)
        box.show()
        box.list.setCurrentRow(0)
        box.list.item(1).setSelected(True)
        box.confirm_selected()
        controller = file_actions(box, facade)
        confirmation = next(
            d for d in controller.dialogs if d.objectName() == "viewClosedConfirmation"
        )
        facade.prepare_open("attachment", items[0].id)
        confirmation.findChild(QCheckBox).setChecked(True)
        next(b for b in confirmation.findChildren(QPushButton) if b.text() == "확인").click()
        qtbot.waitUntil(lambda: controller._job_thread is None)
        assert "닫음 확인 1개 · 실패 1개 · 미처리 0개" in box.feedback.text()
        assert all(path.exists() for path in paths)
        assert sum(copy.closed for copy in facade.cache.page()) == 1
        box.shutdown()


def test_cache_unreadable_metadata_is_not_reported_as_empty(qtbot, tmp_path, monkeypatch):
    with lifecycle(tmp_path) as (env, facade):
        item = original(env, facade)
        path = facade.prepare_open("attachment", item.id)

        def unreadable(*args):
            raise OSError("상태 읽기 불가")

        monkeypatch.setattr(facade.cache, "get", unreadable)
        box = ViewCacheDialog(facade)
        qtbot.addWidget(box)
        assert box.list.count() == 0
        assert "상태 확인 필요 1개" in box.usage_label.full_text
        assert not box.preserve_button.isEnabled() and path.exists()
        box.shutdown()
