from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from officeflow.application.data_inspection import DataInspectionService, InspectionTarget
from officeflow.infrastructure.backup_health import BackupHealthStore


def test_backup_failure_survives_restart_ack_and_later_failure(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    health = BackupHealthStore(path)
    health.begin("automatic")
    health.finish("automatic", "success")
    previous = health.snapshot().attempts[0].last_success_at
    health.begin("automatic")
    health.finish("automatic", "failed", "safe reason")
    restored = BackupHealthStore(path)
    assert restored.snapshot().attention
    assert restored.snapshot().attempts[0].last_success_at == previous
    restored.acknowledge()
    assert not BackupHealthStore(path).snapshot().attention
    restored.begin("automatic")
    restored.finish("automatic", "failed", "safe reason")
    assert restored.snapshot().attention
    restored.begin("manual")
    restored.finish("manual", "success")
    assert restored.snapshot().attention  # manual success does not erase automatic failure
    restored.begin("automatic")
    restored.finish("automatic", "success")
    assert not restored.snapshot().attention


def test_interrupted_canceled_and_broken_metadata(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    health = BackupHealthStore(path)
    health.begin("manual")
    assert not health.snapshot().attention
    assert BackupHealthStore(path).snapshot().attempts[0].status == "unconfirmed"
    assert BackupHealthStore(path).snapshot().attention
    health.finish("manual", "canceled")
    assert not BackupHealthStore(path).snapshot().attention
    path.write_text("{}")
    assert BackupHealthStore(path).snapshot().recording_warning


def test_status_write_failure_does_not_turn_backup_success_into_failure(
    tmp_path: Path, monkeypatch
) -> None:
    import officeflow.infrastructure.backup_health as module

    health = BackupHealthStore(tmp_path / "health.json")

    def fail(*_args):
        raise PermissionError("private text")

    monkeypatch.setattr(module.os, "replace", fail)
    health.begin("automatic")
    health.finish("automatic", "success")
    assert health.snapshot().attempts[0].status == "success"
    assert health.snapshot().attention
    assert "private text" not in health.snapshot().recording_warning


class Repository:
    def __init__(self, count=1):
        self.items = tuple(
            InspectionTarget(f"{index:05}", "aa/file", "name", "첨부", False, 1, "hash")
            for index in range(count)
        )
        self.changed = False
        self.pages = []

    def scope(self):
        return self.items[-1].asset_id, len(self.items)

    def page(self, after, upper):
        values = tuple(item for item in self.items if after < item.asset_id <= upper)[:100]
        self.pages.append(len(values))
        return values

    def current(self, identity):
        item = next(item for item in self.items if item.asset_id == identity)
        return replace(item, checksum="changed") if self.changed else item


class Reader:
    def inspect(self, target, canceled):
        return "modified"


def test_inspection_bounded_pages_details_and_cancel() -> None:
    repository = Repository(625)
    report = DataInspectionService(repository, Reader()).run(lambda: False)
    assert report.checked == 625
    assert report.counts == {"modified": 625}
    assert len(report.issues) == 500 and report.omitted == 125
    assert max(repository.pages) == 100
    stopped = []
    report = DataInspectionService(repository, Reader()).run(
        lambda: bool(stopped), lambda *_: stopped.append(True)
    )
    assert report.canceled and report.checked == 1


def test_change_during_file_read_not_reported_as_normal() -> None:
    repository = Repository()

    class ChangingReader:
        def inspect(self, target, canceled):
            repository.changed = True
            return "normal"

    report = DataInspectionService(repository, ChangingReader()).run(lambda: False)
    assert report.counts == {"unavailable": 1}


def test_retry_and_cancellation_do_not_hide_unacknowledged_failure(tmp_path):
    health = BackupHealthStore(tmp_path / "health.json")
    health.begin("automatic")
    health.finish("automatic", "failed", "safe reason")
    health.begin("automatic")
    assert health.snapshot().attention
    health.finish("automatic", "canceled")
    assert health.snapshot().attention
    health.acknowledge()
    assert not health.snapshot().attention
    assert health.snapshot().attempts[0].last_failure_error == "safe reason"


def test_python_diagnostics_private_values_not_recorded_and_hook_restored(tmp_path, caplog):
    import sys
    import threading

    from officeflow.bootstrap.diagnostics import RuntimeDiagnostics

    previous = sys.excepthook
    previous_thread = threading.excepthook
    diagnostics = RuntimeDiagnostics(tmp_path)
    messages = []
    diagnostics.notify = messages.append
    diagnostics.install()
    diagnostics.install()
    try:
        try:
            raise ValueError("private document secret")
        except ValueError as error:
            diagnostics._exception(type(error), error, error.__traceback__)
        assert "ValueError" in caplog.text
        assert "private document secret" not in caplog.text
        assert len(messages) == 1
    finally:
        diagnostics.close()
    assert sys.excepthook == previous
    assert threading.excepthook == previous_thread


def test_serious_problem_is_not_hidden_after_many_editable_changes():
    repository = Repository(600)

    class MixedReader:
        def inspect(self, target, canceled):
            return "missing" if target.asset_id == "00599" else "modified"

    report = DataInspectionService(repository, MixedReader()).run(lambda: False)
    assert len(report.issues) == 500
    assert report.issues[0].status == "missing"
    assert report.omitted == 100


def test_waiting_cancellation_does_not_overwrite_another_active_backup(tmp_path):
    health = BackupHealthStore(tmp_path / "health.json")
    health.begin("manual")
    stamp = health.snapshot().attempts[0].attempted_at
    health.waiting_canceled("manual")
    assert health.snapshot().attempts[0].status == "running"
    assert health.snapshot().attempts[0].attempted_at == stamp
    health.finish("manual", "success")
    health.waiting_canceled("manual")
    assert health.snapshot().attempts[0].status == "canceled"
