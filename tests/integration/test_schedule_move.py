# ruff: noqa: F811
from datetime import timedelta

import pytest

from officeflow.application.task_saving import ReminderEdit
from officeflow.application.tasks import TaskDraft
from officeflow.domain.enums import ReminderRelation
from officeflow.domain.reminder import ReminderRuleInput as Rule
from tests.integration.test_multiple_reminders import NOW, environment  # noqa: F401


def make(environment, *, repeat=False):
    _, tasks, repo, reminders, save = environment
    start = NOW + timedelta(days=1)
    task = save.save(
        TaskDraft(
            "옮길 업무",
            description="보존",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            recurrence_rule="FREQ=DAILY;COUNT=3" if repeat else None,
        ),
        edits=(
            ReminderEdit(Rule(ReminderRelation.START, offset_minutes=0)),
            ReminderEdit(Rule(ReminderRelation.ABSOLUTE, absolute_at=NOW + timedelta(days=10))),
        ),
        now=NOW,
    )
    return tasks, repo, reminders, save, task


def move(save, repo, task, start, end, *, occurrence=None, now=NOW, command_id=None):
    return save.move_schedule(
        task.id,
        start,
        end,
        task.all_day,
        expected_task=task,
        expected_rules=repo.list_reminders(task.id),
        occurrence_start=occurrence.occurrence_start if occurrence else None,
        expected_occurrence=occurrence,
        now=now,
        command_id=command_id,
    )


def test_regular_relative_moves_absolute_stays_and_stale_rejected(environment):
    tasks, repo, _, save, task = make(environment)
    before = repo.list_reminders(task.id)
    changed = move(
        save, repo, task, task.starts_at + timedelta(days=2), task.ends_at + timedelta(days=2)
    )
    rules = repo.list_reminders(task.id)
    assert (
        next(r for r in rules if r.relation is ReminderRelation.START).next_fire_at
        == changed.starts_at
    )
    assert (
        next(r for r in rules if r.relation is ReminderRelation.ABSOLUTE).next_fire_at
        == next(r for r in before if r.relation is ReminderRelation.ABSOLUTE).next_fire_at
    )
    assert changed.description == task.description
    with pytest.raises(ValueError):
        move(save, repo, task, task.starts_at, task.ends_at)
    assert tasks.get(task.id).starts_at == changed.starts_at


def test_recurrence_moves_into_another_month_and_keeps_original_key(environment):
    tasks, repo, reminders, save, task = make(environment, repeat=True)
    occurrence = tasks._validated_occurrence(task, task.starts_at)
    start, end = task.starts_at + timedelta(days=40), task.ends_at + timedelta(days=40)
    moved = move(save, repo, task, start, end, occurrence=occurrence)
    assert moved.occurrence_start == task.starts_at and moved.starts_at == start
    assert tasks.get(task.id).starts_at == task.starts_at
    original = tasks.calendar_schedule(task.starts_at, task.starts_at + timedelta(days=1))
    assert not original
    actual = tasks.calendar_schedule(start, start + timedelta(days=1))
    assert len(actual) == 1 and actual[0].occurrence_start == task.starts_at
    # Other original occurrences remain, then the moved occurrence is delivered.
    alerts = reminders.poll_due(now=start)
    assert any(
        a.delivery.occurrence_start == task.starts_at and a.delivery.scheduled_at == start
        for a in alerts
    )
    assert not reminders.poll_due(now=start)


def test_collision_rolls_back_occurrence_and_cache(environment):
    tasks, repo, _, save, task = make(environment, repeat=True)
    occurrence = tasks._validated_occurrence(task, task.starts_at)
    rules = repo.list_reminders(task.id)
    with pytest.raises(ValueError, match="같은 시각"):
        move(
            save,
            repo,
            task,
            task.starts_at + timedelta(days=1),
            task.ends_at + timedelta(days=1),
            occurrence=occurrence,
        )
    assert tasks._validated_occurrence(task, task.starts_at).effective_start is None
    assert repo.list_reminders(task.id) == rules


def test_delivered_occurrence_is_not_fired_again_after_move(environment):
    tasks, repo, reminders, save, task = make(environment, repeat=True)
    assert reminders.poll_due(now=task.starts_at)
    occurrence = tasks._validated_occurrence(task, task.starts_at)
    new_start = task.starts_at + timedelta(days=4)
    move(
        save,
        repo,
        task,
        new_start,
        task.ends_at + timedelta(days=4),
        occurrence=occurrence,
        now=task.starts_at,
    )
    assert all(
        a.delivery.occurrence_start != task.starts_at for a in reminders.poll_due(now=new_start)
    )


def test_replay_is_single_move(environment):
    _tasks, repo, _, save, task = make(environment)
    rules = repo.list_reminders(task.id)
    args = dict(
        expected_task=task, expected_rules=rules, now=NOW, command_id="schedule-move-replay"
    )
    first = save.move_schedule(
        task.id, task.starts_at + timedelta(days=3), task.ends_at + timedelta(days=3), False, **args
    )
    replay = save.move_schedule(
        task.id, task.starts_at + timedelta(days=3), task.ends_at + timedelta(days=3), False, **args
    )
    assert replay.command_replayed and replay.starts_at == first.starts_at


def test_moved_occurrence_agrees_in_month_day_today_and_preview(environment):
    from officeflow.application.day_overview import DayOverviewService
    from officeflow.application.reminder_preview import ReminderPreviewService
    from officeflow.application.tasks import TaskQuery, TaskView

    tasks, repo, reminders, save, task = make(environment, repeat=True)
    occurrence = tasks._validated_occurrence(task, task.starts_at)
    start = task.starts_at + timedelta(days=40)
    move(save, repo, task, start, task.ends_at + timedelta(days=40), occurrence=occurrence)
    overview = tasks.calendar_overview(start.date(), start.date() + timedelta(days=1))
    assert overview.total == 1 and overview.preview_tasks[0].occurrence_start == task.starts_at
    day = DayOverviewService(tasks).query(start.date(), timezone="UTC")
    assert len(day.items) == 1 and day.items[0].occurrence_start == task.starts_at
    today = tasks.query(TaskQuery(view=TaskView.TODAY), now=start)
    assert any(t.id == task.id and t.occurrence_start == task.starts_at for t in today.items)
    preview = ReminderPreviewService(repo, reminders).query(
        start.date(), start.date(), timezone="UTC", now=NOW
    )
    assert len(preview.items) == 1 and preview.items[0].scheduled_at == start


def test_rule_edit_and_completed_occurrence_conflicts_leave_schedule(environment):
    from officeflow.domain.enums import OccurrenceStatus

    tasks, repo, _, save, task = make(environment, repeat=True)
    occurrence = tasks._validated_occurrence(task, task.starts_at)
    tasks.transition_occurrence(task.id, task.starts_at, OccurrenceStatus.COMPLETED, now=NOW)
    with pytest.raises(ValueError, match="회차"):
        move(
            save,
            repo,
            task,
            task.starts_at + timedelta(days=5),
            task.ends_at + timedelta(days=5),
            occurrence=occurrence,
        )
    assert tasks._validated_occurrence(task, task.starts_at).effective_start is None


def test_past_relative_alarm_and_id_mismatch_are_atomic(environment):
    tasks, repo, _, save, task = make(environment)
    before = repo.list_reminders(task.id)
    with pytest.raises(ValueError):
        move(save, repo, task, NOW - timedelta(days=1), NOW - timedelta(hours=23))
    assert tasks.get(task.id).starts_at == task.starts_at
    assert repo.list_reminders(task.id) == before
    other = tasks.create(TaskDraft("잘못 선택한 업무"), now=NOW)
    with pytest.raises(ValueError, match="일치"):
        save.move_schedule(
            other.id,
            task.starts_at,
            task.ends_at,
            False,
            expected_task=task,
            expected_rules=before,
            now=NOW,
        )


def test_move_undo_history_and_delivery_are_not_rewritten(environment):
    from sqlalchemy import select

    from officeflow.infrastructure.database.models import TaskHistoryRecord

    sessions, _, _, _, _ = environment
    tasks, repo, reminders, save, task = make(environment, repeat=True)
    occurrence = tasks._validated_occurrence(task, task.starts_at)
    # Fire earlier than the original date, then return to the original date.
    moved = move(
        save, repo, task, NOW + timedelta(hours=1), NOW + timedelta(hours=2), occurrence=occurrence
    )
    alert = reminders.poll_due(now=NOW + timedelta(hours=1))[0]
    undone = move(
        save,
        repo,
        task,
        occurrence.starts_at,
        occurrence.ends_at,
        occurrence=moved,
        now=NOW + timedelta(hours=2),
    )
    assert undone.starts_at == task.starts_at
    assert all(
        a.delivery.occurrence_start != task.starts_at
        for a in reminders.poll_due(now=task.starts_at)
    )
    assert repo.get_reminder_delivery(alert.delivery.id) is not None
    with sessions.transaction() as session:
        rows = tuple(
            session.scalars(
                select(TaskHistoryRecord).where(
                    TaskHistoryRecord.task_id == task.id,
                    TaskHistoryRecord.scope == "occurrence",
                    TaskHistoryRecord.event_type == "change",
                )
            )
        )
    assert len(rows) == 2 and all(row.occurrence_start == task.starts_at for row in rows)


def test_runtime_summary_uses_moved_date_without_query_during_label(environment, monkeypatch):
    from officeflow.presentation.dates import format_datetime
    from officeflow.presentation.reminder_settings import reminder_summary
    from tests.integration.test_multiple_reminders import edits_for

    tasks, repo, reminders, save, task = make(environment, repeat=True)
    occurrence = tasks._validated_occurrence(task, task.starts_at)
    start = NOW + timedelta(hours=1)
    move(save, repo, task, start, start + timedelta(hours=1), occurrence=occurrence)
    runtime = reminders.runtime_state(task.id)
    edits = edits_for(repo, task)
    monkeypatch.setattr(repo, "moved_occurrences", lambda *_a: pytest.fail("라벨 계산은 I/O 금지"))
    text = reminder_summary(edits, task, NOW, runtime=runtime)
    assert format_datetime(start, task.timezone, short=True) in text


def test_one_candidate_poll_budget_makes_progress(environment):
    _tasks, _repo, reminders, _save, task = make(environment, repeat=True)
    assert reminders.poll_due(now=task.starts_at, max_candidates=1, max_poll_seconds=10)
    assert not reminders.poll_due(now=task.starts_at, max_candidates=1, max_poll_seconds=10)


def test_old_move_history_does_not_project_task_after_recurrence_removed(environment):
    tasks, repo, _, save, task = make(environment, repeat=True)
    occurrence = tasks._validated_occurrence(task, task.starts_at)
    moved_start = task.starts_at + timedelta(days=40)
    move(save, repo, task, moved_start, moved_start + timedelta(hours=1), occurrence=occurrence)
    tasks.update(
        task.id,
        TaskDraft("일반 업무로 전환", starts_at=task.starts_at + timedelta(days=100)),
        now=NOW,
    )
    assert tasks.calendar_schedule(moved_start, moved_start + timedelta(days=1)) == ()


def test_stale_claim_rejected_even_when_next_rule_cache_is_same(environment):
    from dataclasses import replace

    from officeflow.application.reminders import ReminderTarget
    from officeflow.domain.enums import OccurrenceStatus
    from officeflow.domain.reminder import ReminderDelivery

    tasks, repo, _, save, task = make(environment, repeat=True)
    key = task.starts_at + timedelta(days=1)
    occurrence = tasks._validated_occurrence(task, key)
    rule = next(r for r in repo.list_reminders(task.id) if r.relation is ReminderRelation.START)
    target = ReminderTarget(rule, task)
    move(
        save,
        repo,
        task,
        key + timedelta(days=4),
        occurrence.ends_at + timedelta(days=4),
        occurrence=occurrence,
    )
    # Moving a later occurrence leaves this rule's first occurrence cache intact.
    current_rule = next(r for r in repo.list_reminders(task.id) if r.id == rule.id)
    assert current_rule.next_fire_at == rule.next_fire_at
    alert = ReminderDelivery.fired(
        reminder_id=rule.id,
        task_id=task.id,
        occurrence_start=key,
        scheduled_at=key,
        fire_key="stale-moved-claim",
        now=key,
    )
    assert repo.claim_current_delivery(alert, target) is None
    tasks.transition_occurrence(
        task.id,
        task.starts_at,
        OccurrenceStatus.COMPLETED,
        now=NOW,
    )
    closed = replace(
        alert,
        occurrence_start=task.starts_at,
        scheduled_at=task.starts_at,
        fire_key="stale-closed-claim",
    )
    assert repo.claim_current_delivery(closed, target) is None
