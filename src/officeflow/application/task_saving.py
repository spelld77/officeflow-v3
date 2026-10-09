"""One transaction for task details, reminder rules and their schedules."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from officeflow.application.reminders import ReminderService, ReminderTarget
from officeflow.application.task_mutations import tracked_command
from officeflow.application.tasks import TaskDraft, TaskService
from officeflow.domain.enums import ReminderRelation, TaskStatus
from officeflow.domain.reminder import Reminder, ReminderRuleInput
from officeflow.domain.task import Task


@dataclass(frozen=True, slots=True)
class ReminderEdit:
    rule: ReminderRuleInput
    reminder_id: int | None = None


class EditableReminderRepository(Protocol):
    def list_reminders(self, task_id: int) -> tuple[Reminder, ...]: ...
    def apply_edits(
        self, task_id: int, edits: tuple[ReminderEdit, ...], *, now: datetime
    ) -> tuple[Reminder, ...]: ...
    def save_reminder_schedule(
        self,
        reminder_id: int,
        next_fire_at: datetime | None,
        next_occurrence_start: datetime | None,
    ) -> None: ...
    def dismiss_rule_deliveries(self, reminder_id: int, now: datetime) -> None: ...
    def restore_reminder_schedule(self, reminder: Reminder) -> None: ...


@dataclass(frozen=True, slots=True)
class TaskSaveContext:
    tasks: TaskService
    reminders: ReminderService
    repository: EditableReminderRepository


class TaskSaveService:
    def __init__(
        self,
        unit_of_work: Callable[[], AbstractContextManager[TaskSaveContext]],
        *,
        mutation_runner: Any = None,
        command_context: Any = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._mutation_runner = mutation_runner
        self._command_context = command_context

    def _for_mutation(self, context: Any) -> TaskSaveService:
        @contextmanager
        def scoped_work() -> Iterator[TaskSaveContext]:
            tasks = TaskService(context.task_repository, command_context=context)
            yield TaskSaveContext(
                tasks,
                ReminderService(context.reminder_repository, tasks, command_context=context),
                context.reminder_repository,
            )

        return TaskSaveService(scoped_work, command_context=context)

    @tracked_command
    def save(
        self,
        draft: TaskDraft,
        *,
        task_id: int | None = None,
        edits: tuple[ReminderEdit, ...] | None = None,
        expected_task: Task | None = None,
        expected_rules: tuple[Reminder, ...] | None = None,
        now: datetime | None = None,
        command_id: str | None = None,
    ) -> Task:
        current = now or datetime.now(UTC)
        edits = (
            edits
            if edits is not None
            else tuple(ReminderEdit(rule) for rule in draft.reminder_rules)
        )
        with self._unit_of_work() as work:
            previous = work.tasks.get(task_id) if task_id is not None else None
            existing = work.repository.list_reminders(task_id) if task_id is not None else ()
            if (
                expected_task is not None
                and previous is not None
                and (
                    previous.updated_at != expected_task.updated_at
                    or (
                        previous.title,
                        previous.description,
                        previous.status,
                        previous.priority,
                        previous.starts_at,
                        previous.ends_at,
                        previous.all_day,
                        previous.recurrence_rule,
                        previous.timezone,
                        previous.is_pinned,
                        previous.result_note,
                    )
                    != (
                        expected_task.title,
                        expected_task.description,
                        expected_task.status,
                        expected_task.priority,
                        expected_task.starts_at,
                        expected_task.ends_at,
                        expected_task.all_day,
                        expected_task.recurrence_rule,
                        expected_task.timezone,
                        expected_task.is_pinned,
                        expected_task.result_note,
                    )
                )
            ):
                raise ValueError(
                    "업무가 다른 창에서 변경되었습니다. 입력을 보존하고 다시 확인하세요."
                )
            if expected_rules is not None and {(r.id, r.identity, r.enabled) for r in existing} != {
                (r.id, r.identity, r.enabled) for r in expected_rules
            }:
                raise ValueError("알림 설정이 다른 창에서 변경되었습니다. 다시 확인하세요.")
            if len(edits) > 20 and (
                len(edits) > len(existing) or any(e.reminder_id is None for e in edits)
            ):
                raise ValueError("업무 하나에 알림을 최대 20개까지 설정할 수 있습니다.")
            identities = [edit.rule.identity for edit in edits]
            if len(identities) != len(set(identities)):
                raise ValueError("동일한 알림을 중복으로 설정할 수 없습니다.")
            before = {r.id: r for r in existing}
            seen_ids: set[int] = set()
            for edit in edits:
                if edit.reminder_id is not None:
                    if edit.reminder_id not in before or edit.reminder_id in seen_ids:
                        raise ValueError("이 업무의 올바른 알림을 선택하세요.")
                    seen_ids.add(edit.reminder_id)
                if edit.rule.relation is ReminderRelation.START and draft.starts_at is None:
                    raise ValueError("시작 기준 알림을 제거하거나 날짜 지정으로 변경하세요.")
                if edit.rule.relation is ReminderRelation.END and draft.ends_at is None:
                    raise ValueError("종료 기준 알림을 제거하거나 날짜 지정으로 변경하세요.")
            saved = (
                work.tasks.create(draft, now=current)
                if task_id is None
                else work.tasks.update(task_id, draft, now=current)
            )
            assert saved.id is not None
            rules = work.repository.apply_edits(saved.id, edits, now=current)
            minute = current.replace(second=0, microsecond=0)
            due_times: dict[datetime, bool] = {}
            for rule in rules:
                old = before.get(rule.id)
                relation = rule.relation
                base_changed = (
                    previous is not None
                    and relation is not ReminderRelation.ABSOLUTE
                    and (
                        (
                            relation is ReminderRelation.START
                            and previous.starts_at != saved.starts_at
                        )
                        or (relation is ReminderRelation.END and previous.ends_at != saved.ends_at)
                        or (
                            bool(previous.recurrence_rule or saved.recurrence_rule)
                            and previous.starts_at != saved.starts_at
                        )
                        or previous.timezone != saved.timezone
                        or previous.recurrence_rule != saved.recurrence_rule
                    )
                )
                reopened = (
                    previous is not None
                    and previous.status not in {TaskStatus.ACTIVE, TaskStatus.PENDING}
                    and saved.status in {TaskStatus.ACTIVE, TaskStatus.PENDING}
                )
                changed = (
                    old is None
                    or old.identity != rule.identity
                    or old.enabled != rule.enabled
                    or base_changed
                    or reopened
                )
                target = ReminderTarget(rule, saved)
                candidate = (
                    work.reminders._next_candidate(target, after=minute, inclusive=True)
                    if rule.enabled
                    else None
                )
                if saved.status not in {TaskStatus.ACTIVE, TaskStatus.PENDING}:
                    candidate = None
                if (
                    changed
                    and not reopened
                    and rule.enabled
                    and saved.status in {TaskStatus.ACTIVE, TaskStatus.PENDING}
                    and candidate is None
                ):
                    raise ValueError("새 알림은 현재 분 이후의 유효한 예약으로 지정하세요.")
                if candidate is not None:
                    if candidate[1] in due_times and (changed or due_times[candidate[1]]):
                        raise ValueError("같은 시각에 겹치는 알림이 있습니다.")
                    due_times[candidate[1]] = changed
                if changed:
                    if base_changed and old is not None:
                        assert rule.id is not None
                        work.repository.dismiss_rule_deliveries(rule.id, current)
                    assert rule.id is not None
                    work.repository.save_reminder_schedule(
                        rule.id,
                        candidate[1] if candidate else None,
                        candidate[0] if candidate else None,
                    )
                elif old is not None and (
                    rule.next_fire_at,
                    rule.next_occurrence_start,
                    rule.schedule_initialized,
                ) != (old.next_fire_at, old.next_occurrence_start, old.schedule_initialized):
                    # The legacy task-update trigger resets every rule. Restore
                    # only untouched rules inside this same write transaction.
                    work.repository.restore_reminder_schedule(old)
            return saved
