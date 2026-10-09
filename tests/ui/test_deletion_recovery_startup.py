"""The actual startup wires receipt-aware recovery after first-window construction."""

from threading import Event, get_ident

from officeflow.application.attachment_deletion import (
    AttachmentDeletionService,
    DeleteCommand,
    DeleteTarget,
)
from officeflow.application.tasks import TaskDraft
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.attachments.deletion_storage import DeletionFileStorage
from officeflow.infrastructure.database.attachment_deletion_repository import (
    SqlAlchemyDeletionRepository,
)
from tests.integration.test_attachment_search import search_environment


def test_startup_delete_recovery_is_worker_owned_and_does_not_block_input(
    qtbot, qapp, tmp_path, monkeypatch
):
    from uuid import uuid4

    from officeflow.infrastructure.settings.store import JsonSettingsStore
    from officeflow.main import build_application

    paths = AppPaths(tmp_path)
    paths.ensure_directories()
    JsonSettingsStore(paths.settings_file).save(JsonSettingsStore(paths.settings_file).load())
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        task = env.tasks.create(TaskDraft(title="복구할 업무"))
        file = tmp_path / "file.txt"
        file.write_bytes(b"restore me")
        source = env.attachments.attach(task.id, file)
        repository = SqlAlchemyDeletionRepository(env.sessions)
        storage = DeletionFileStorage(paths.attachment_dir, coordinator=env.attachments.coordinator)
        plan = repository.plan(DeleteTarget("attachment", source.id))
        cmd = DeleteCommand(uuid4().hex, plan.target, plan.fingerprint)
        storage.prepare(cmd, plan, lambda: False)
        original = paths.attachment_dir / source.relative_path
        assert not original.exists()
    monkeypatch.setattr(AppPaths, "discover", classmethod(lambda _cls: paths))
    entered, release = Event(), Event()
    threads = []
    recover = AttachmentDeletionService.recover

    def hold(self, *args, **kwargs):
        threads.append(get_ident())
        entered.set()
        assert release.wait(10)
        return recover(self, *args, **kwargs)

    monkeypatch.setattr(AttachmentDeletionService, "recover", hold)
    _, window = build_application(application=qapp)
    qtbot.addWidget(window)
    assert not entered.is_set() and not original.exists()
    window.show()
    try:
        qtbot.waitUntil(entered.is_set)
        assert window.isVisible() and window._search.isEnabled()
        assert threads != [get_ident()]
        assert window._task_service.create(TaskDraft(title="복구 중 업무 입력")).id
        release.set()
        qtbot.waitUntil(lambda: window._internal_recovery_thread is None)
        assert original.read_bytes() == b"restore me"
        assert "완료 확인 1개" in window.statusBar().currentMessage()
    finally:
        release.set()
        window.shutdown()
