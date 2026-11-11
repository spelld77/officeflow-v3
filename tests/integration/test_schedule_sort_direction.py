from datetime import UTC, datetime, timedelta

import pytest

from officeflow.application.tasks import TaskDraft, TaskQuery, TaskSort, TaskView


@pytest.mark.parametrize("view", [TaskView.ALL, TaskView.UPCOMING])
def test_schedule_latest_first_before_pagination_preserves_pinned(task_service, view):
    now = datetime(2026, 11, 11, 0, tzinfo=UTC)
    near = task_service.create(TaskDraft(title="Near", starts_at=now + timedelta(days=1)))
    far = task_service.create(TaskDraft(title="Far", starts_at=now + timedelta(days=10)))
    pinned = task_service.create(
        TaskDraft(title="Pinned", starts_at=now + timedelta(days=2), is_pinned=True)
    )
    no_date = task_service.create(TaskDraft(title="No date"))
    past = task_service.create(TaskDraft(title="Past", starts_at=now - timedelta(days=10)))
    first = task_service.query(TaskQuery(view=view, limit=2), now=now)
    second = task_service.query(TaskQuery(view=view, offset=2, limit=10), now=now)
    assert [item.id for item in first.items] == [pinned.id, far.id]
    expected = [near.id, past.id, no_date.id] if view is TaskView.ALL else [near.id]
    assert [item.id for item in second.items] == expected


def test_other_views_and_explicit_sort_keep_their_order(task_service):
    now = datetime(2026, 11, 11, 0, tzinfo=UTC)
    near = task_service.create(TaskDraft(title="A", starts_at=now + timedelta(hours=1)))
    far = task_service.create(TaskDraft(title="Z", starts_at=now + timedelta(hours=2)))
    today = task_service.query(TaskQuery(view=TaskView.TODAY), now=now)
    assert [item.id for item in today.items] == [near.id, far.id]
    for view in (TaskView.ALL, TaskView.UPCOMING):
        page = task_service.query(TaskQuery(view=view, sort=TaskSort.TITLE), now=now)
        assert [item.title for item in page.items] == sorted(item.title for item in page.items)
