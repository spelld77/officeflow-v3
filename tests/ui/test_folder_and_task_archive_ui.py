import json
import os
from datetime import date
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QFontDatabase, QPalette
from PySide6.QtQml import QJSEngine
from PySide6.QtWidgets import QDialogButtonBox, QScrollArea

from officeflow.application.folder_links import FolderLinkService
from officeflow.application.records import RecordService
from officeflow.application.task_archive import ArchiveScope
from officeflow.infrastructure.database.folder_link_repository import SqlAlchemyFolderLinkRepository
from officeflow.presentation.folder_links import FolderLinksPanel
from officeflow.presentation.record_dialog import TaskRecordsDialog
from officeflow.presentation.task_archive_dialog import TaskArchiveDialog
from tests.integration.test_attachment_export import files  # noqa: F401
from tests.unit.test_record_service import InMemoryRecordRepository


def link_service(environment):
    _tasks, _task, _attachments, repo, _managed, storage, _exports, _folder = environment
    return FolderLinkService(SqlAlchemyFolderLinkRepository(repo._sessions), (storage.data_root,))


def test_folder_tab_without_attachment_service_maps_correctly_and_readonly(files, qtbot):  # noqa: F811
    tasks, task, *_ = files
    widget = TaskRecordsDialog(
        task,
        task_service=tasks,
        record_service=RecordService(InMemoryRecordRepository(), tasks),
        folder_link_service=link_service(files),
        initial_tab="folders",
    )
    qtbot.addWidget(widget)
    widget.show()
    assert widget.current_tab_key == "folders" and widget.tabs.count() == 4
    widget.select_tab("attachments")
    assert widget.current_tab_key == "folders"
    widget.shutdown()


def test_folder_worker_delivers_open_on_gui_thread_and_failure_keeps_link(
    files,  # noqa: F811
    qtbot,
    monkeypatch,
):
    *_, folder = files
    service = link_service(files)
    link = service.save(files[1].id, "업무", folder)
    panel = FolderLinksPanel(service, files[1].id)
    qtbot.addWidget(panel)
    panel.show()
    delivered = []
    from PySide6.QtCore import QCoreApplication, QThread

    from officeflow.presentation import folder_links

    monkeypatch.setattr(
        folder_links.QDesktopServices,
        "openUrl",
        lambda _url: (
            delivered.append(QThread.currentThread() == QCoreApplication.instance().thread())
            or True
        ),
    )
    panel.open_selected()
    qtbot.waitUntil(lambda: panel._job_thread is None, timeout=10000)
    assert delivered == [True]
    folder.rename(folder.with_name("moved"))
    panel.open_selected()
    qtbot.waitUntil(lambda: panel._job_thread is None, timeout=10000)
    assert "연결은 유지" in panel.status.text()
    assert service.list(files[1].id)[0].id == link.id
    panel.shutdown()


def test_folder_shutdown_waits_and_does_not_dispose_worker(files, qtbot):  # noqa: F811
    from threading import Event

    started = Event()
    panel = FolderLinksPanel(link_service(files), files[1].id)
    qtbot.addWidget(panel)

    def operation(canceled):
        started.set()
        while not canceled():
            Event().wait(0.01)
        return None

    panel._run(operation)
    qtbot.waitUntil(started.is_set, timeout=10000)
    panel.shutdown()
    qtbot.waitUntil(lambda: panel._job_thread is None, timeout=10000)


def test_archive_scope_options_and_narrow_footer_scroll(qtbot):
    dialog = TaskArchiveDialog((1, 2))
    qtbot.addWidget(dialog)
    dialog.resize(490, 370)
    dialog.show()
    assert dialog.options().start == date(date.today().year, 1, 1)
    dialog.scope.setCurrentIndex(1)
    assert dialog.start.isEnabled() and not dialog.year.isEnabled()
    dialog.scope.setCurrentIndex(2)
    assert dialog.options().scope == ArchiveScope.SELECTED
    assert not dialog.basis.isEnabled() and not dialog.completed.isEnabled()
    box = dialog.findChild(QDialogButtonBox)
    assert dialog.rect().contains(box.geometry())
    scroll = dialog.findChild(QScrollArea)
    assert scroll.verticalScrollBar().maximum() > 0


@pytest.mark.parametrize("dark", [False, True])
def test_new_ui_explicit_light_palette_and_capture(files, qtbot, qapp, dark):  # noqa: F811
    if os.environ.get("OFFICEFLOW_RENDER_FONTS") == "1":
        for font in ("malgun.ttf", "malgunbd.ttf", "segoeui.ttf"):
            assert QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / font)) >= 0
    previous = qapp.palette()
    if dark:
        palette = QPalette()
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#202020"))
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
        ):
            palette.setColor(role, QColor("#ffffff"))
        qapp.setPalette(palette)
    service = link_service(files)
    service.save(files[1].id, "긴 업무 폴더 이름 " * 8, files[-1])
    panel = FolderLinksPanel(service, files[1].id)
    dialog = TaskArchiveDialog((files[1].id,))
    qtbot.addWidget(panel)
    qtbot.addWidget(dialog)
    panel.resize(500, 420)
    dialog.resize(500, 570)
    panel.show()
    dialog.show()
    try:
        qtbot.wait(30)
        assert panel.grab().toImage().pixelColor(3, 3).lightness() > 150
        assert panel.list.palette().color(QPalette.ColorRole.Base).lightness() > 150
        assert panel.width() <= 500
        assert dialog.grab().toImage().pixelColor(3, 3).lightness() > 150
        for button in panel.buttons.values():
            assert panel.rect().contains(button.geometry())
        directory = Path("artifacts/folder-archive-ui-captures")
        directory.mkdir(parents=True, exist_ok=True)
        scale = os.environ.get("QT_SCALE_FACTOR", "1")
        for name, widget in (("folders", panel), ("archive", dialog)):
            assert widget.grab().save(
                str(directory / f"{name}-{'dark' if dark else 'light'}-{scale}.png")
            )
    finally:
        panel.shutdown()
        qapp.setPalette(previous)


def test_real_exported_html_script_searches_collapsed_content_and_clear(qtbot, tmp_path):
    from datetime import UTC, datetime

    from officeflow.application.task_archive import ArchiveOptions, ArchiveTask
    from officeflow.infrastructure.exports.task_archive import HtmlExcelArchiveWriter

    writer = HtmlExcelArchiveWriter()
    writer.start(tmp_path, ArchiveOptions(date(2026, 1, 1), date(2026, 12, 31)), datetime.now(UTC))
    try:
        writer.task(
            ArchiveTask(1, "업무", "completed", "normal", "hidden-marker", "", "", "", (), ()),
            datetime.now(UTC),
            (),
        )
        writer.finish()
    finally:
        writer.close()
    script = (
        (tmp_path / "업무목록.html")
        .read_text(encoding="utf-8")
        .split("<script>")[1]
        .split("</script>")[0]
    )
    engine = QJSEngine()
    tasks = [
        {"textContent": "업무 hidden-marker", "dataset": {"status": "completed"}, "open": True}
    ]
    engine.evaluate(
        "var testTasks=" + json.dumps(tasks) + "; var controls={};"
        "['query','status','count','clear'].forEach(k=>controls[k]={value:'',addEventListener:function(k,fn){this[k]=fn;},focus:function(){}});"
        "var document={documentElement:{classList:{add:function(){}}},querySelectorAll:function(){return testTasks;},getElementById:function(id){return controls[id];}};"
    )
    result = engine.evaluate(script)
    assert not result.isError(), result.toString()
    assert not engine.evaluate("testTasks[0].open").toBool()
    engine.evaluate("controls.query.value='HIDDEN-MARKER';filter();")
    assert not engine.evaluate("testTasks[0].hidden").toBool()
    engine.evaluate("controls.query.value='not-found';filter();")
    assert engine.evaluate("testTasks[0].hidden").toBool()
    engine.evaluate("controls.clear.click();")
    assert not engine.evaluate("testTasks[0].hidden").toBool()


def test_bootstrap_menu_data_export_and_main_input_stay_available(
    qtbot, qapp, tmp_path, monkeypatch
):
    from threading import Event

    from PySide6.QtWidgets import QDialog, QFileDialog

    from officeflow.application.task_archive import ArchiveOptions
    from officeflow.application.tasks import TaskDraft
    from officeflow.bootstrap.paths import AppPaths
    from officeflow.main import build_application
    from officeflow.presentation.data_dialog import DataManagementDialog

    paths = AppPaths(tmp_path / "profile")
    monkeypatch.setattr(AppPaths, "discover", classmethod(lambda _cls: paths))
    _app, window = build_application(application=qapp, hourly_notifications_enabled=False)
    qtbot.addWidget(window)
    window.show()
    task = window._task_service.create(TaskDraft("묶음 UI 확인"))
    menu = window._build_task_context_menu(task, calendar_actions=True)
    assert "폴더 바로가기…" in [action.text() for action in menu.actions()]
    menu.deleteLater()
    window._show_task_records(task, initial_tab="folders")
    dialog = next(box for box in window._file_dialogs if isinstance(box, TaskRecordsDialog))
    assert dialog.current_tab_key == "folders"
    dialog.reject()
    window._open_data_management()
    data = next(box for box in window._file_dialogs if isinstance(box, DataManagementDialog))
    qtbot.waitUntil(lambda: data._thread is None, timeout=10000)
    outside = tmp_path / "archive"
    outside.mkdir()
    options = ArchiveOptions(
        date(2026, 1, 1),
        date(2026, 12, 31),
        scope=ArchiveScope.SELECTED,
        task_ids=(task.id,),
        attachments=False,
    )
    monkeypatch.setattr(TaskArchiveDialog, "exec", lambda _self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(TaskArchiveDialog, "options", lambda _self: options)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args: str(outside))
    started, release = Event(), Event()
    export = window._task_archive_service.export

    def held(parent, options, **kwargs):
        started.set()
        assert release.wait(10)
        return export(parent, options, **kwargs)

    monkeypatch.setattr(window._task_archive_service, "export", held)
    try:
        data._export_task_archive()
        qtbot.waitUntil(started.is_set, timeout=10000)
        assert not data.isModal() and qapp.activeModalWidget() is None
        window._search.setText("입력 가능")
        assert window._search.text() == "입력 가능"
        release.set()
        qtbot.waitUntil(lambda: data._thread is None, timeout=10000)
        assert "보관 완료" in data.status_label.text()
        assert data._archive_folder_button.isEnabled()
        assert (data._archive_result.folder / "완료.txt").exists()
    finally:
        release.set()
        window.shutdown()
