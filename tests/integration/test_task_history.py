from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from officeflow.application.reminders import ReminderService
from officeflow.application.task_saving import ReminderEdit
from officeflow.application.tasks import TaskDraft, TaskService
from officeflow.domain.enums import OccurrenceStatus, ReminderRelation, TaskStatus
from officeflow.domain.reminder import ReminderRuleInput
from officeflow.infrastructure.database.models import TaskHistoryRecord, TaskMutationReceiptRecord
from officeflow.infrastructure.database.reminder_repository import SqlAlchemyReminderRepository
from officeflow.infrastructure.database.task_saving import task_save_service


def test_create_change_and_noop_are_semantic(task_service: TaskService) -> None:
    task = task_service.create(TaskDraft("예산", description="100만원"))
    task_service.update(task.id, TaskDraft("예산", description="100(천원)"))
    task_service.update(task.id, TaskDraft("예산", description="100(천원)"))
    task_service.complete(task.id, result_note="제출 완료")
    history = task_service.history.page(task.id)
    assert len(history.items) == 3
    change = task_service.history.get(task.id, history.items[0].id)
    assert set(change.changed_fields) == {"status", "result_note"}
    assert change.before["description"] == "100(천원)"
    assert change.after["result_note"] == "제출 완료"


def test_history_failure_rolls_back_task(task_service: TaskService, monkeypatch) -> None:
    task = task_service.create(TaskDraft("変更前"))
    from officeflow.infrastructure.database.task_history_repository import HistoryWriter

    def fail(*args, **kwargs):
        raise OSError("history failed")

    monkeypatch.setattr(HistoryWriter, "append", fail)
    with pytest.raises(OSError):
        task_service.transition(task.id, TaskStatus.COMPLETED)
    assert task_service.get(task.id).status is TaskStatus.ACTIVE


def test_retry_and_id_conflict(task_service: TaskService) -> None:
    task = task_service.create(TaskDraft("first"), command_id="a" * 32)
    task_service.update(task.id, TaskDraft("second"))
    replay = task_service.create(TaskDraft("first"), command_id="a" * 32)
    assert replay.id == task.id and replay.command_replayed
    assert replay.title == "second"  # explicitly current value, not the original result
    with pytest.raises(ValueError, match="다른"):
        task_service.create(TaskDraft("other"), command_id="a" * 32)
    assert len(task_service.history.page(task.id).items) == 2


def test_first_legacy_change_baseline_only_on_success(task_service: TaskService) -> None:
    repository = task_service._repository
    from officeflow.domain.task import Task

    task = repository.add(Task.create(title="legacy", now=datetime.now(UTC)))
    task_service.update(task.id, TaskDraft("legacy"))
    assert not task_service.history.page(task.id).items
    task_service.update(task.id, TaskDraft("changed"))
    events = task_service.history.page(task.id).items
    assert {item.event_type for item in events} == {"baseline", "change"}
    with repository._sessions.transaction() as session:
        assert session.scalar(select(func.count()).select_from(TaskHistoryRecord)) == 2


def test_occurrence_cache_and_rule_change_preserve_original_identity(task_service):
    now = datetime.now(UTC).replace(microsecond=0)
    draft = TaskDraft("repeat", starts_at=now, recurrence_rule="FREQ=DAILY;COUNT=3")
    task = task_service.create(draft, now=now)
    task_service.ensure_occurrence(task.id, now)
    assert len(task_service.history.page(task.id).items) == 1
    from officeflow.domain.occurrence import TaskOccurrence

    now = now + timedelta(microseconds=4321)
    task_service._repository.save_occurrence(TaskOccurrence(None, task.id, now, None))
    task_service.transition_occurrence(
        task.id, now, OccurrenceStatus.COMPLETED, result_note="result", now=now
    )
    task_service.update(task.id, replace(draft, recurrence_rule="FREQ=DAILY;COUNT=1"), now=now)
    items = task_service.history.page(task.id).items
    occurrence = [item for item in items if item.occurrence_start is not None]
    assert len(occurrence) == 2
    assert all("004321Z" in item.scope_key for item in occurrence)
    change = next(item for item in occurrence if item.event_type == "change")
    assert set(change.changed_fields) == {"status", "result_note"}
    assert task_service.history.get(task.id, change.id).after["result_note"] == "result"
    with pytest.raises(ValueError, match="같은 회차"):
        task_service.history.compare(task.id, change.id, items[0].id)


def test_task_save_rule_validation_failure_rolls_back_everything(task_service):
    sessions = task_service._repository._sessions
    save = task_save_service(sessions, "Asia/Seoul")
    now = datetime.now(UTC)
    task = save.save(TaskDraft("old"), now=now)
    invalid = ReminderEdit(
        ReminderRuleInput(ReminderRelation.ABSOLUTE, absolute_at=now - timedelta(days=1))
    )
    with pytest.raises(ValueError, match="현재 분"):
        save.save(TaskDraft("bad"), task_id=task.id, edits=(invalid,), now=now)
    assert task_service.get(task.id).title == "old"
    assert len(task_service.history.page(task.id).items) == 1
    assert not SqlAlchemyReminderRepository(sessions).list_reminders(task.id)


@pytest.mark.parametrize("action", ["complete", "defer"])
@pytest.mark.parametrize("failure", ["history", "delivery", "receipt"])
def test_reminder_task_history_delivery_and_receipt_atomic(
    task_service, monkeypatch, action, failure
):
    from sqlalchemy import event

    from officeflow.domain.enums import ReminderDeliveryStatus
    from officeflow.infrastructure.database.task_history_repository import HistoryWriter

    sessions = task_service._repository._sessions
    repo = SqlAlchemyReminderRepository(sessions)
    reminders = ReminderService(repo, task_service)
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    task = task_service.create(TaskDraft("due", starts_at=now), now=now)
    reminders.replace_rules(task.id, (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),))
    alert = reminders.poll_due(now=now)[0]

    def fail(*args, **kwargs):
        raise OSError("synthetic failure")

    def receipt_fail(_mapper, _connection, _target):
        fail()

    if failure == "history":
        monkeypatch.setattr(HistoryWriter, "append", fail)
    elif failure == "delivery":
        monkeypatch.setattr(SqlAlchemyReminderRepository, "save_reminder_delivery", fail)
    else:
        event.listen(TaskMutationReceiptRecord, "before_insert", receipt_fail)
    try:
        with pytest.raises(OSError, match="synthetic"):
            getattr(reminders, action)(alert.delivery.id, now=now)
    finally:
        if failure == "receipt":
            event.remove(TaskMutationReceiptRecord, "before_insert", receipt_fail)
    assert task_service.get(task.id).status is TaskStatus.ACTIVE
    assert repo.get_reminder_delivery(alert.delivery.id).status is ReminderDeliveryStatus.FIRED
    assert len(task_service.history.page(task.id).items) == 1


def test_noop_retry_does_not_apply_again_and_note_conflict(task_service):
    task = task_service.create(TaskDraft("stable"))
    command_id = "b" * 32
    task_service.update(task.id, TaskDraft("stable"), command_id=command_id)
    task_service.update(task.id, TaskDraft("new"))
    replay = task_service.update(task.id, TaskDraft("stable"), command_id=command_id)
    assert replay.title == "new" and replay.command_replayed
    task_service.update_result_note(task.id, "first", expected_note="")
    with pytest.raises(ValueError, match="다른 창"):
        task_service.update_result_note(task.id, "second", expected_note="")
    assert task_service.result_note(task.id) == "first"


def test_history_is_preserved_in_trash_and_delete_requires_consent(task_service):
    task = task_service.create(TaskDraft("retained"))
    task_service.move_to_trash(task.id)
    assert len(task_service.history.page(task.id).items) == 1
    with pytest.raises(ValueError, match="이력"):
        task_service.delete_permanently(task.id)
    assert task_service.get_including_deleted(task.id).deleted_at is not None
    task_service.delete_permanently(task.id, retained_approved=True)
    assert not task_service.history.page(task.id).items


def test_history_and_receipt_full_backup_restore(tmp_path):
    from officeflow.bootstrap.paths import AppPaths
    from officeflow.infrastructure.backup import BackupManager
    from tests.integration.test_attachment_search import search_environment

    paths = AppPaths(tmp_path / "app")
    paths.ensure_directories()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        task = env.tasks.create(TaskDraft("before"), command_id="c" * 32)
        env.tasks.update(task.id, TaskDraft("after"))
        backup = BackupManager(paths).create_backup()
        assert backup.table_counts["task_history"] == 2
        assert backup.table_counts["task_mutation_receipts"] == 2
        BackupManager(paths).stage_restore(backup.path)
    BackupManager(paths).apply_pending_restore()
    with search_environment(paths.data_dir, attachment_root=paths.attachment_dir) as env:
        assert len(env.tasks.history.page(task.id).items) == 2
        replay = env.tasks.create(TaskDraft("before"), command_id="c" * 32)
        assert replay.command_replayed and replay.title == "after"


def test_pending_completion_undo_is_one_atomic_semantic_event(task_service):
    task = task_service.create(TaskDraft("undo", status=TaskStatus.PENDING))
    task_service.complete(task.id)
    before_count = len(task_service.history.page(task.id).items)
    result = task_service.undo_completion(task.id, TaskStatus.PENDING, command_id="undo-1")
    assert result.status is TaskStatus.PENDING
    assert len(task_service.history.page(task.id).items) == before_count + 1
    entry = task_service.history.get(task.id, task_service.history.page(task.id).items[0].id)
    assert entry.before["status"]["value"] == "completed"
    assert entry.after["status"]["value"] == "pending"
    assert task_service.undo_completion(
        task.id, TaskStatus.PENDING, command_id="undo-1"
    ).command_replayed
