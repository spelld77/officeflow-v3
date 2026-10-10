from dataclasses import replace
from datetime import UTC, datetime, timedelta

from officeflow.application.reminder_groups import group_reminders, merge_day_reminders
from officeflow.application.reminder_preview import ReminderPreviewItem
from officeflow.application.tasks import ScheduledTask
from officeflow.domain.task import Task

NOW = datetime(2026, 10, 10, 1, tzinfo=UTC)


def alarm(task, identifier, *, occurrence=None, status="예정", minutes=0):
    due = NOW + timedelta(minutes=minutes)
    return ReminderPreviewItem(task, identifier, occurrence, due, due, status)


def test_nonrecurring_relative_and_absolute_merge_without_losing_rules():
    task = replace(Task.create(title="같은 업무", starts_at=NOW), id=1)
    first = alarm(task, 1, occurrence=NOW)
    second = alarm(task, 2)
    groups = group_reminders((first, second, first))
    assert len(groups) == 1
    assert groups[0].key == (1, None)
    assert groups[0].items == (first, second)
    merged = merge_day_reminders((ScheduledTask(task, NOW, None),), (first, second))
    assert len(merged) == 1 and merged[0].scheduled.starts_at == NOW


def test_same_title_different_tasks_and_original_occurrences_stay_separate():
    task = replace(
        Task.create(title="같은 제목", starts_at=NOW, recurrence_rule="FREQ=DAILY"), id=1
    )
    other = replace(task, id=2)
    groups = group_reminders(
        (
            alarm(task, 1, occurrence=NOW),
            alarm(task, 2, occurrence=NOW + timedelta(days=1)),
            alarm(task, 3),
            alarm(other, 4, occurrence=NOW),
        )
    )
    assert len(groups) == 4
    assert len({group.key for group in groups}) == 4


def test_schedule_free_alarm_does_not_move_task_schedule():
    task = replace(Task.create(title="다음 달 업무", starts_at=NOW + timedelta(days=30)), id=1)
    item = alarm(task, 1)
    merged = merge_day_reminders((), (item,))
    assert len(merged) == 1 and merged[0].scheduled is None
    assert merged[0].task.starts_at == NOW + timedelta(days=30)


def test_pending_representative_does_not_hide_attention_or_history():
    task = replace(Task.create(title="상태 혼합"), id=1)
    items = (
        alarm(task, 1, status="확인함"),
        alarm(task, 2, minutes=20),
        alarm(task, 3, status="미확인", minutes=5),
        alarm(task, 4, status="시각 지남 · 발생 기록 없음", minutes=10),
    )
    group = group_reminders(items)[0]
    assert group.representative.reminder_id == 3
    assert len(group.items) == 4
    assert group.pending_count == 2 and group.attention_count == 1


def test_moved_occurrence_matches_original_key_and_no_alarm_schedule_stays_plain():
    task = replace(
        Task.create(title="이동 회차", starts_at=NOW, recurrence_rule="FREQ=DAILY"), id=1
    )
    moved = ScheduledTask(task, NOW + timedelta(days=2), None, occurrence_start=NOW)
    another = ScheduledTask(
        task, NOW + timedelta(days=3), None, occurrence_start=NOW + timedelta(days=1)
    )
    merged = merge_day_reminders((moved, another), (alarm(task, 1, occurrence=NOW),))
    assert len(merged) == 2
    assert merged[0].scheduled is moved
    assert merged[1] is another
