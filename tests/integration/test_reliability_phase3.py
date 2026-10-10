from datetime import timedelta

from sqlalchemy import select

from officeflow.application.reminder_groups import group_reminders, merge_day_reminders
from officeflow.application.reminder_preview import ReminderPreviewService
from officeflow.application.task_saving import ReminderEdit
from officeflow.application.tasks import ScheduledTask, TaskDraft
from officeflow.domain.enums import ReminderDeliveryStatus, ReminderRelation, TaskStatus
from officeflow.domain.reminder import ReminderRuleInput as Rule
from officeflow.infrastructure.database.models import ReminderDeliveryRecord
from tests.integration.test_multiple_reminders import NOW, absolute
from tests.integration.test_multiple_reminders import environment as environment


def deliveries(sessions):
    with sessions.transaction() as session:
        rows = session.scalars(
            select(ReminderDeliveryRecord).order_by(ReminderDeliveryRecord.id)
        ).all()
        return [(r.id, r.status, r.scheduled_at, r.snoozed_until, r.occurrence_start) for r in rows]


def test_real_nonrecurring_relative_absolute_are_one_row_and_query_never_fires(environment):
    sessions, _tasks, repo, reminders, save = environment
    task = save.save(
        TaskDraft("당일 알림", starts_at=NOW),
        edits=(ReminderEdit(Rule(ReminderRelation.START, offset_minutes=0)), absolute(minutes=20)),
        now=NOW,
    )
    before = deliveries(sessions)
    result = ReminderPreviewService(repo, reminders).query(
        NOW.date(), NOW.date(), timezone="UTC", now=NOW
    )
    assert len(result.items) == 2
    assert len({item.occurrence_start for item in result.items}) == 2
    rows = merge_day_reminders((ScheduledTask(task, NOW, None),), result.items)
    assert len(rows) == 1 and len(rows[0].items) == 2
    assert deliveries(sessions) == before == []


def test_real_two_occurrences_and_common_absolute_same_day_are_not_merged(environment):
    sessions, _tasks, repo, reminders, save = environment
    save.save(
        TaskDraft("반복 회차", starts_at=NOW, recurrence_rule="FREQ=DAILY;COUNT=3"),
        edits=(
            ReminderEdit(Rule(ReminderRelation.START, offset_minutes=0)),
            ReminderEdit(Rule(ReminderRelation.START, offset_minutes=-1430)),
            absolute(minutes=20),
        ),
        now=NOW,
    )
    result = ReminderPreviewService(repo, reminders).query(
        NOW.date(), NOW.date(), timezone="UTC", now=NOW
    )
    groups = group_reminders(result.items)
    assert {g.occurrence_start for g in groups} == {None, NOW, NOW + timedelta(days=1)}
    assert len(groups) == 3 and len(result.items) == 3
    assert deliveries(sessions) == []


def test_real_ack_snooze_completion_remain_per_delivery_after_grouping(environment):
    sessions, tasks, repo, reminders, save = environment
    task = save.save(
        TaskDraft("독립 처리", starts_at=NOW),
        edits=(absolute(), absolute(minutes=10), absolute(minutes=20)),
        now=NOW,
    )
    first = reminders.poll_due(now=NOW)[0]
    reminders.acknowledge(first.delivery.id, now=NOW)
    second = reminders.poll_due(now=NOW + timedelta(minutes=10))[0]
    reminders.snooze(second.delivery.id, 1440, now=NOW + timedelta(minutes=10))
    before = deliveries(sessions)
    result = ReminderPreviewService(repo, reminders).query(
        NOW.date(),
        (NOW + timedelta(days=1)).date(),
        timezone="UTC",
        now=NOW + timedelta(minutes=11),
    )
    original = group_reminders(result.for_date(NOW.date(), "UTC"))[0]
    next_day = group_reminders(result.for_date((NOW + timedelta(days=1)).date(), "UTC"))[0]
    assert original.pending_count == 1 and len(original.items) == 3
    assert next_day.pending_count == 1 and len(next_day.items) == 1
    assert deliveries(sessions) == before
    third = reminders.poll_due(now=NOW + timedelta(minutes=20))[0]
    reminders.complete(third.delivery.id, now=NOW + timedelta(minutes=20))
    before = deliveries(sessions)
    result = ReminderPreviewService(repo, reminders).query(
        NOW.date(),
        (NOW + timedelta(days=1)).date(),
        timezone="UTC",
        now=NOW + timedelta(minutes=21),
    )
    groups = group_reminders(result.items)
    assert len(groups) == 1 and groups[0].pending_count == 0
    assert tasks.get(task.id).status is TaskStatus.COMPLETED
    assert (
        repo.get_reminder_delivery(first.delivery.id).status is ReminderDeliveryStatus.ACKNOWLEDGED
    )
    assert repo.get_reminder_delivery(second.delivery.id).status is ReminderDeliveryStatus.COMPLETED
    assert deliveries(sessions) == before


def test_period_end_alarm_group_uses_task_scope_not_end_day_as_identity(environment):
    _sessions, _tasks, repo, reminders, save = environment
    task = save.save(
        TaskDraft("기간 업무", starts_at=NOW - timedelta(days=1), ends_at=NOW + timedelta(hours=3)),
        edits=(ReminderEdit(Rule(ReminderRelation.END, offset_minutes=0)), absolute(minutes=60)),
        now=NOW,
    )
    result = ReminderPreviewService(repo, reminders).query(
        NOW.date(), NOW.date(), timezone="UTC", now=NOW
    )
    rows = merge_day_reminders((ScheduledTask(task, task.starts_at, task.ends_at),), result.items)
    assert len(rows) == 1 and rows[0].key == (task.id, None)
    assert len(rows[0].items) == 2
