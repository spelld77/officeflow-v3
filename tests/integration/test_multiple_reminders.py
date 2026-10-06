from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from officeflow.application.reminder_preview import ReminderPreviewService
from officeflow.application.reminders import ReminderService
from officeflow.application.task_saving import ReminderEdit
from officeflow.application.tasks import TaskDraft, TaskService
from officeflow.domain.enums import ReminderDeliveryStatus, ReminderRelation, TaskStatus
from officeflow.domain.reminder import ReminderRuleInput as Rule
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.models import ReminderDeliveryRecord, TaskRecord
from officeflow.infrastructure.database.reminder_repository import SqlAlchemyReminderRepository
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository
from officeflow.infrastructure.database.task_saving import task_save_service

NOW = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)


@pytest.fixture
def environment(tmp_path):
    database = tmp_path / "test.db"
    upgrade_database(database)
    engine = create_database_engine(database)
    sessions = SessionFactory(engine)
    tasks = TaskService(SqlAlchemyTaskRepository(sessions), timezone="Asia/Seoul")
    repo = SqlAlchemyReminderRepository(sessions)
    reminders = ReminderService(repo, tasks)
    yield sessions, tasks, repo, reminders, task_save_service(sessions, "Asia/Seoul")
    engine.dispose()


def absolute(days=0, minutes=0):
    return ReminderEdit(
        Rule(ReminderRelation.ABSOLUTE, absolute_at=NOW + timedelta(days=days, minutes=minutes))
    )


def edits_for(repo, task):
    return tuple(
        ReminderEdit(Rule(r.relation, r.offset_minutes, r.absolute_at, r.enabled), r.id)
        for r in repo.list_reminders(task.id)
    )


def test_three_reminders_one_task_and_independent_confirmation(environment):
    sessions, _tasks, repo, reminders, save = environment
    task = save.save(
        TaskDraft("30일 뒤 업무", starts_at=NOW + timedelta(days=30)),
        edits=(absolute(), absolute(10), absolute(20)),
        now=NOW,
    )
    assert len(repo.list_reminders(task.id)) == 3
    alerts = reminders.poll_due(now=NOW)
    assert len(alerts) == 1
    reminders.acknowledge(alerts[0].delivery.id, now=NOW)
    assert len(reminders.poll_due(now=NOW + timedelta(days=10))) == 1
    with sessions.transaction() as session:
        assert session.scalar(select(func.count()).select_from(TaskRecord)) == 1


@pytest.mark.parametrize("failure", ["duplicate", "past", "missing_start", "limit", "collision"])
def test_failed_save_rolls_back_task_and_rules(environment, failure):
    sessions, _tasks, _repo, _reminders, save = environment
    edits = (
        (absolute(), absolute())
        if failure == "duplicate"
        else (absolute(-1),)
        if failure == "past"
        else (ReminderEdit(Rule(ReminderRelation.START, offset_minutes=0)),)
        if failure == "missing_start"
        else tuple(absolute(i) for i in range(21))
        if failure == "limit"
        else (absolute(), ReminderEdit(Rule(ReminderRelation.START, offset_minutes=0)))
    )
    draft = TaskDraft("실패 업무", starts_at=NOW if failure == "collision" else None)
    with pytest.raises(ValueError):
        save.save(draft, edits=edits, now=NOW)
    with sessions.transaction() as session:
        assert session.scalar(select(func.count()).select_from(TaskRecord)) == 0


def test_unchanged_save_preserves_id_snooze_cache_and_history(environment):
    _sessions, tasks, repo, reminders, save = environment
    draft = TaskDraft("원본", starts_at=NOW)
    task = save.save(draft, edits=(absolute(), absolute(10)), now=NOW)
    alert = reminders.poll_due(now=NOW)[0]
    snooze = reminders.snooze(alert.delivery.id, 60, now=NOW)
    original = repo.list_reminders(task.id)
    save.save(
        replace(draft, title="제목만 수정"),
        task_id=task.id,
        edits=edits_for(repo, task),
        expected_task=tasks.get(task.id),
        expected_rules=original,
        now=NOW + timedelta(minutes=1),
    )
    assert repo.list_reminders(task.id) == original
    assert repo.get_reminder_delivery(snooze.id) == snooze


def test_edit_keeps_id_and_closes_old_snooze(environment):
    _sessions, _tasks, repo, reminders, save = environment
    draft = TaskDraft("수정", starts_at=NOW)
    task = save.save(draft, edits=(absolute(),), now=NOW)
    alert = reminders.poll_due(now=NOW)[0]
    snooze = reminders.snooze(alert.delivery.id, 60, now=NOW)
    original = repo.list_reminders(task.id)[0]
    edit = ReminderEdit(absolute(2).rule, original.id)
    save.save(draft, task_id=task.id, edits=(edit,), now=NOW)
    assert repo.list_reminders(task.id)[0].id == original.id
    assert repo.get_reminder_delivery(snooze.id).status is ReminderDeliveryStatus.ACKNOWLEDGED


def test_preview_future_task_no_schedule_and_no_writes(environment):
    sessions, _tasks, repo, reminders, save = environment
    save.save(
        TaskDraft("미래 업무", starts_at=NOW + timedelta(days=30)), edits=(absolute(),), now=NOW
    )
    save.save(TaskDraft("일정 없는 업무"), edits=(absolute(minutes=60),), now=NOW)
    before = repo.list_enabled_reminder_targets()
    preview = ReminderPreviewService(repo, reminders).query(
        NOW.date(), NOW.date(), timezone="Asia/Seoul", now=NOW
    )
    assert {i.task.title for i in preview.items} == {"미래 업무", "일정 없는 업무"}
    assert preview.complete
    assert repo.list_enabled_reminder_targets() == before
    with sessions.transaction() as session:
        assert session.scalar(select(func.count()).select_from(ReminderDeliveryRecord)) == 0


def test_completion_closes_all_same_task_deliveries(environment):
    _sessions, tasks, repo, reminders, save = environment
    task = save.save(
        TaskDraft("완료", starts_at=NOW), edits=(absolute(), absolute(minutes=1)), now=NOW
    )
    first = reminders.poll_due(now=NOW)[0]
    reminders.snooze(first.delivery.id, 60, now=NOW)
    second = reminders.poll_due(now=NOW + timedelta(minutes=1))[0]
    reminders.complete(second.delivery.id, now=NOW + timedelta(minutes=1))
    assert tasks.get(task.id).status is TaskStatus.COMPLETED
    assert repo.get_reminder_delivery(first.delivery.id).status is ReminderDeliveryStatus.COMPLETED
    assert reminders.active_snoozes() == ()


def test_cross_day_snooze_has_reference_on_both_dates(environment):
    _sessions, _tasks, repo, reminders, save = environment
    save.save(TaskDraft("미루기"), edits=(absolute(),), now=NOW)
    alert = reminders.poll_due(now=NOW)[0]
    reminders.snooze(alert.delivery.id, 1440, now=NOW)
    result = ReminderPreviewService(repo, reminders).query(
        NOW.date(), NOW.date() + timedelta(days=1), timezone="Asia/Seoul", now=NOW
    )
    assert result.for_date(NOW.date(), "Asia/Seoul")[0].status == "다른 날로 미룸"
    assert result.for_date(NOW.date() + timedelta(days=1), "Asia/Seoul")[0].status == "재알림"


def test_stale_task_or_rule_snapshot_rejected_with_inputs_preserved(environment):
    _sessions, tasks, repo, _reminders, save = environment
    draft = TaskDraft("첫 창")
    task = save.save(draft, edits=(absolute(1),), now=NOW)
    original_rules = repo.list_reminders(task.id)
    save.save(
        replace(draft, title="다른 창 수정"), task_id=task.id, edits=edits_for(repo, task), now=NOW
    )
    with pytest.raises(ValueError, match="다른 창"):
        save.save(draft, task_id=task.id, edits=edits_for(repo, task), expected_task=task, now=NOW)
    latest = tasks.get(task.id)
    save.save(
        replace(draft, title=latest.title),
        task_id=task.id,
        edits=(ReminderEdit(absolute(2).rule, original_rules[0].id),),
        now=NOW,
    )
    with pytest.raises(ValueError, match="알림 설정"):
        save.save(
            draft,
            task_id=task.id,
            edits=edits_for(repo, task),
            expected_rules=original_rules,
            now=NOW,
        )


def test_relative_schedule_moves_absolute_and_unrelated_snooze_do_not(environment):
    _sessions, _tasks, repo, reminders, save = environment
    draft = TaskDraft("일정 이동", starts_at=NOW, ends_at=NOW + timedelta(hours=2))
    task = save.save(
        draft,
        edits=(
            absolute(),
            ReminderEdit(Rule(ReminderRelation.START, offset_minutes=10)),
            ReminderEdit(Rule(ReminderRelation.END, offset_minutes=-60)),
        ),
        now=NOW,
    )
    fired = reminders.poll_due(now=NOW)[0]
    snooze = reminders.snooze(fired.delivery.id, 60, now=NOW)
    before = {r.relation: r for r in repo.list_reminders(task.id)}
    save.save(
        replace(draft, starts_at=NOW + timedelta(minutes=30)),
        task_id=task.id,
        edits=edits_for(repo, task),
        now=NOW,
    )
    after = {r.relation: r for r in repo.list_reminders(task.id)}
    assert after[ReminderRelation.ABSOLUTE] == before[ReminderRelation.ABSOLUTE]
    assert after[ReminderRelation.END] == before[ReminderRelation.END]
    assert after[ReminderRelation.START].next_fire_at == NOW + timedelta(minutes=40)
    assert repo.get_reminder_delivery(snooze.id) == snooze


def test_preview_multiple_recurring_occurrences_and_closed_scope(environment):
    _sessions, tasks, repo, reminders, save = environment
    from officeflow.domain.enums import OccurrenceStatus

    draft = TaskDraft("반복", starts_at=NOW, recurrence_rule="FREQ=DAILY;COUNT=4")
    task = save.save(
        draft,
        edits=(ReminderEdit(Rule(ReminderRelation.START, offset_minutes=0)), absolute(2)),
        now=NOW,
    )
    tasks.transition_occurrence(
        task.id, NOW + timedelta(days=1), OccurrenceStatus.COMPLETED, now=NOW
    )
    result = ReminderPreviewService(repo, reminders).query(
        NOW.date(), NOW.date() + timedelta(days=4), timezone="Asia/Seoul", now=NOW
    )
    assert len(result.items) == 4
    assert len(result.for_date(NOW.date() + timedelta(days=1), "Asia/Seoul")) == 0
    direct = next(i for i in result.items if i.occurrence_start is None)
    assert direct.scheduled_at == NOW + timedelta(days=2)


def test_preview_budget_reports_partial_and_cancellation(environment):
    _sessions, _tasks, repo, reminders, save = environment
    save.save(TaskDraft("다수"), edits=tuple(absolute(minutes=i) for i in range(20)), now=NOW)
    preview = ReminderPreviewService(repo, reminders)
    result = preview.query(NOW.date(), NOW.date(), timezone="Asia/Seoul", now=NOW, limit=3)
    assert not result.complete and len(result.items) == 3
    canceled = preview.query(
        NOW.date(), NOW.date(), timezone="Asia/Seoul", now=NOW, cancel_requested=lambda: True
    )
    assert not canceled.complete


def test_cache_and_claim_compare_current_rule_before_writing(environment):
    _sessions, _tasks, repo, _reminders, save = environment
    task = save.save(TaskDraft("경쟁"), edits=(absolute(),), now=NOW)
    old = repo.get_reminder_target(repo.list_reminders(task.id)[0].id)
    save.save(
        TaskDraft("경쟁"),
        task_id=task.id,
        edits=(ReminderEdit(absolute(2).rule, old.reminder.id),),
        now=NOW,
    )
    assert not repo.save_schedule_if_current(old, NOW + timedelta(days=8), None)
    from officeflow.domain.reminder import ReminderDelivery

    delivery = ReminderDelivery.fired(
        reminder_id=old.reminder.id,
        task_id=task.id,
        occurrence_start=None,
        scheduled_at=NOW,
        fire_key="stale-test",
        now=NOW,
    )
    assert repo.claim_current_delivery(delivery, old) is None
    assert repo.list_reminders(task.id)[0].next_fire_at == NOW + timedelta(days=2)


def test_trash_restore_does_not_revive_old_snoozes_or_missed_schedules(environment):
    _sessions, tasks, _repo, reminders, save = environment
    task = save.save(
        TaskDraft("복원"), edits=(absolute(), absolute(minutes=30), absolute(2)), now=NOW
    )
    first = reminders.poll_due(now=NOW)[0]
    reminders.snooze(first.delivery.id, 120, now=NOW)
    tasks.move_to_trash(task.id, now=NOW + timedelta(minutes=1))
    tasks.restore_from_trash(task.id, now=NOW + timedelta(minutes=60))
    assert reminders.poll_due(now=NOW + timedelta(minutes=61), recover_unhandled=True) == ()
    assert len(reminders.poll_due(now=NOW + timedelta(days=2))) == 1


def test_failure_after_rule_edit_rolls_back_previous_title_and_deliveries(environment):
    _sessions, tasks, repo, reminders, save = environment
    task = save.save(TaskDraft("보호"), edits=(absolute(), absolute(2)), now=NOW)
    first = reminders.poll_due(now=NOW)[0]
    snooze = reminders.snooze(first.delivery.id, 120, now=NOW)
    original = repo.list_reminders(task.id)
    bad_edits = (
        ReminderEdit(absolute(-1).rule, original[0].id),
        ReminderEdit(absolute(2).rule, original[1].id),
    )
    with pytest.raises(ValueError):
        save.save(TaskDraft("잘못 수정"), task_id=task.id, edits=bad_edits, now=NOW)
    assert tasks.get(task.id).title == "보호"
    assert repo.list_reminders(task.id) == original
    assert repo.get_reminder_delivery(snooze.id) == snooze


def test_legacy_more_than_twenty_preserved_and_editable_but_not_expandable(environment):
    sessions, tasks, repo, _reminders, save = environment
    from officeflow.infrastructure.database.models import ReminderRecord

    task = tasks.create(TaskDraft("이전 자료"), now=NOW)
    with sessions.transaction() as session:
        for i in range(21):
            session.add(
                ReminderRecord(
                    task_id=task.id,
                    relation="absolute",
                    absolute_at=NOW + timedelta(days=i + 1),
                    enabled=False,
                )
            )
    original = repo.list_reminders(task.id)
    edits = edits_for(repo, task)
    save.save(TaskDraft("제목 변경"), task_id=task.id, edits=edits, now=NOW)
    assert repo.list_reminders(task.id) == original
    edited = (replace(edits[0], rule=absolute(30).rule), *edits[1:])
    save.save(TaskDraft("항목 수정"), task_id=task.id, edits=edited, now=NOW)
    assert len(repo.list_reminders(task.id)) == 21
    with pytest.raises(ValueError, match="20개"):
        save.save(TaskDraft("추가 거절"), task_id=task.id, edits=(*edited, absolute(40)), now=NOW)


def test_multiple_relative_twenty_ten_one_days_before(environment):
    _sessions, _tasks, repo, reminders, save = environment
    start = NOW + timedelta(days=30)
    task = save.save(
        TaskDraft("기한 업무", starts_at=start),
        edits=tuple(
            ReminderEdit(Rule(ReminderRelation.START, offset_minutes=-days * 1440))
            for days in (20, 10, 1)
        ),
        now=NOW,
    )
    for days_from_now in (10, 20, 29):
        due = NOW + timedelta(days=days_from_now)
        alerts = reminders.poll_due(now=due)
        assert len(alerts) == 1 and alerts[0].task.id == task.id
        reminders.acknowledge(alerts[0].delivery.id, now=due)
    assert len(repo.list_reminders(task.id)) == 3
