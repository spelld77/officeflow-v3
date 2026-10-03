from datetime import UTC, datetime, timedelta

import pytest
from dateutil.rrule import rrulestr

from officeflow.application.tasks import TaskDraft
from officeflow.domain.enums import OccurrenceStatus, ReminderRelation
from officeflow.domain.recurrence import next_recurrence_start
from officeflow.domain.reminder import ReminderRuleInput
from tests.unit.test_reminder_service import build_services


def test_old_daily_cache_seeks_window_without_walking_400_days(monkeypatch):
    tasks, service, repo = build_services()
    start = datetime(2025, 9, 1, 9, tzinfo=UTC)
    now = start + timedelta(days=400)
    task = tasks.create(
        TaskDraft(title="장기 미실행", starts_at=start, recurrence_rule="FREQ=DAILY"), now=start
    )
    service.replace_rules(
        task.id, (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),), now=start
    )
    original = service._next_candidate
    calls = []

    def counted(*args, **kwargs):
        calls.append(kwargs["after"])
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_next_candidate", counted)
    alerts = service.poll_due(now=now, max_poll_seconds=10)
    assert len(alerts) == 1 and alerts[0].delivery.scheduled_at == now
    assert len(calls) == 2
    assert calls[0] == now - timedelta(hours=2)
    assert repo.list_reminders(task.id)[0].next_fire_at == now + timedelta(days=1)
    assert not service.poll_pending


def test_budget_defers_work_without_losing_or_duplicating_deliveries():
    tasks, service, repo = build_services()
    start = datetime(2026, 10, 3, 1, tzinfo=UTC)
    now = start + timedelta(hours=8)
    task = tasks.create(
        TaskDraft(title="검사 예산", starts_at=start, recurrence_rule="FREQ=HOURLY"), now=start
    )
    service.replace_rules(
        task.id, (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),), now=start
    )
    alerts = []
    for _ in range(10):
        alerts.extend(service.poll_due(now=now, max_candidates=2, max_poll_seconds=10))
        if not service.poll_pending:
            break
    assert [alert.delivery.scheduled_at for alert in alerts] == [
        now - timedelta(hours=2),
        now - timedelta(hours=1),
        now,
    ]
    assert len({alert.delivery.fire_key for alert in alerts}) == 3
    assert service.poll_due(now=now) == ()
    assert repo.list_reminders(task.id)[0].next_fire_at > now


def test_cancelled_poll_keeps_due_cursor_and_can_resume():
    tasks, service, repo = build_services()
    now = datetime(2026, 10, 3, 1, tzinfo=UTC)
    task = tasks.create(TaskDraft(title="취소", starts_at=now))
    service.replace_rules(task.id, (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),))
    before = repo.list_reminders(task.id)
    assert service.poll_due(now=now, cancel_requested=lambda: True) == ()
    assert repo.list_reminders(task.id) == before and service.poll_pending
    assert len(service.poll_due(now=now, max_poll_seconds=10)) == 1


def test_seek_excludes_closed_occurrence_and_finished_rule():
    tasks, service, _repo = build_services()
    start = datetime(2025, 9, 1, 9, tzinfo=UTC)
    now = start + timedelta(days=400)
    task = tasks.create(
        TaskDraft(title="닫힌 회차", starts_at=start, recurrence_rule="FREQ=DAILY"), now=start
    )
    service.replace_rules(
        task.id, (ReminderRuleInput(ReminderRelation.START, offset_minutes=0),), now=start
    )
    tasks.transition_occurrence(task.id, now, OccurrenceStatus.COMPLETED, now=now)
    assert service.poll_due(now=now, max_poll_seconds=10) == ()
    assert not service.poll_pending


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=DAILY;INTERVAL=3",
        "FREQ=WEEKLY;COUNT=8",
        "FREQ=HOURLY;INTERVAL=2",
        "FREQ=MINUTELY;COUNT=40",
        "FREQ=DAILY;UNTIL=20261010T000000Z",
    ],
)
@pytest.mark.parametrize("inclusive", [False, True])
def test_simple_seek_matches_dateutil_reference(rule, inclusive):
    start = datetime(2026, 10, 1, tzinfo=UTC)
    for after in (
        start - timedelta(days=1),
        start,
        start + timedelta(minutes=20),
        start + timedelta(days=25),
    ):
        expected = rrulestr(rule, dtstart=start).after(after, inc=inclusive)
        assert (
            next_recurrence_start(
                rule, template_start=start, timezone="UTC", after=after, inclusive=inclusive
            )
            == expected
        )


def test_twenty_year_minutely_seek_does_not_iterate_history():
    start = datetime(2006, 10, 1, tzinfo=UTC)
    now = datetime(2026, 10, 1, tzinfo=UTC)
    assert next_recurrence_start(
        "FREQ=MINUTELY", template_start=start, timezone="Asia/Seoul", after=now
    ) == now + timedelta(minutes=1)


def test_simple_seek_matches_dateutil_for_fractional_template_start():
    start = datetime(2026, 10, 1, 9, microsecond=56789, tzinfo=UTC)
    after = start + timedelta(days=2)
    expected = rrulestr("FREQ=DAILY", dtstart=start).after(after)
    assert (
        next_recurrence_start("FREQ=DAILY", template_start=start, timezone="UTC", after=after)
        == expected
    )


@pytest.mark.parametrize("interval", [0, -1])
def test_invalid_interval_is_rejected_without_entering_an_endless_rule(interval):
    from officeflow.domain.recurrence import RecurrenceValidationError

    start = datetime(2026, 10, 1, tzinfo=UTC)
    with pytest.raises(RecurrenceValidationError):
        next_recurrence_start(
            f"FREQ=DAILY;INTERVAL={interval}", template_start=start, timezone="UTC", after=start
        )


@pytest.mark.parametrize("timezone", ["America/New_York", "Europe/Berlin"])
def test_simple_daily_seek_preserves_local_clock_across_dst(timezone):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(timezone)
    start = datetime(2026, 3, 1, 9, tzinfo=zone)
    after = datetime(2026, 3, 30, 0, tzinfo=zone)
    expected = rrulestr("FREQ=DAILY", dtstart=start).after(after).astimezone(UTC)
    assert (
        next_recurrence_start(
            "FREQ=DAILY", template_start=start.astimezone(UTC), timezone=timezone, after=after
        )
        == expected
    )
