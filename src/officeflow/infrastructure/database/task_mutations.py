"""One SQLite write UOW for a semantic command, reminders, history and receipt."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from officeflow.application.tasks import TaskService
from officeflow.domain.occurrence import TaskOccurrence
from officeflow.domain.reminder import ReminderDelivery
from officeflow.domain.task import Task
from officeflow.domain.task_history import (
    FIELD_LABELS,
    FIELDS,
    ReplayedNote,
    encode,
    fingerprint,
    scope_key,
    task_values,
)
from officeflow.infrastructure.database.models import TaskHistoryRecord, TaskMutationReceiptRecord
from officeflow.infrastructure.database.reminder_repository import SqlAlchemyReminderRepository
from officeflow.infrastructure.database.session import SessionFactory
from officeflow.infrastructure.database.task_history_repository import HistoryWriter
from officeflow.infrastructure.database.task_repository import SqlAlchemyTaskRepository


class TaskMutationContext:
    def __init__(self, sessions: SessionFactory, command_id: str, current: datetime) -> None:
        self.sessions = sessions
        self.command_id = command_id
        self.current = current
        self.task_repository = SqlAlchemyTaskRepository(sessions)
        self.reminder_repository = SqlAlchemyReminderRepository(sessions)
        self.before: dict[tuple[int, datetime | None], dict[str, Any] | None] = {}
        self.history_ids: list[int] = []
        self.result_scope: tuple[int, datetime | None] | None = None

    def values(self, task_id: int, occurrence_start: datetime | None) -> dict[str, Any]:
        tasks = TaskService(self.task_repository, command_context=self)
        task = tasks.get(task_id)
        occurrence = (
            tasks._validated_occurrence(task, occurrence_start) if occurrence_start else None
        )
        return task_values(task, occurrence)

    def watch(self, task_id: int, occurrence_start: datetime | None) -> None:
        key = (task_id, occurrence_start)
        if key not in self.before:
            self.before[key] = self.values(*key)
        self.result_scope = key

    def observe_result(self, result: Any, action: str) -> None:
        if isinstance(result, Task) and result.id is not None:
            self.result_scope = (result.id, None)
            if action == "create":
                self.before.setdefault(self.result_scope, None)
        elif isinstance(result, TaskOccurrence):
            self.result_scope = (result.task_id, result.occurrence_start)

    def flush(self) -> None:
        writer = HistoryWriter(self.sessions)
        for (task_id, occurrence_start), before in self.before.items():
            after = self.values(task_id, occurrence_start)
            watched = FIELDS if occurrence_start is None else ("schedule", "status", "result_note")
            changed = tuple(
                field for field in watched if before is None or before[field] != after[field]
            )
            if not changed:
                continue
            key = scope_key(occurrence_start)
            base = dict(
                task_id=task_id,
                scope="occurrence" if occurrence_start else "task",
                scope_key=key,
                occurrence_start=occurrence_start,
                command_id=self.command_id,
                recorded_at=self.current,
                payload_version=1,
            )
            with self.sessions.transaction() as session:
                existing = session.scalar(
                    select(TaskHistoryRecord.id)
                    .where(TaskHistoryRecord.task_id == task_id, TaskHistoryRecord.scope_key == key)
                    .limit(1)
                )
            if existing is None and before is not None:
                self.history_ids.append(
                    writer.append(
                        **base,
                        event_type="baseline",
                        changed_fields="[]",
                        before_payload=None,
                        after_payload=encode(before),
                        summary="이력 기록 시작",
                    )
                )
            self.history_ids.append(
                writer.append(
                    **base,
                    event_type="created" if before is None else "change",
                    changed_fields=encode(changed),
                    before_payload=encode(before) if before is not None else None,
                    after_payload=encode(after),
                    summary="업무 생성"
                    if before is None
                    else "·".join(FIELD_LABELS[field] for field in changed),
                )
            )


class TaskMutationRunner:
    def __init__(self, sessions: SessionFactory) -> None:
        self.sessions = sessions

    def execute(
        self,
        owner: Any,
        kind: str,
        request: dict[str, Any],
        current: datetime,
        command_id: str | None,
        operation: Callable[[Any], Any],
    ) -> Any:
        identity = command_id or uuid4().hex
        if not identity or len(identity) > 64:
            raise ValueError("올바른 처리 식별자가 필요합니다.")
        request_hash = fingerprint(
            {"owner": type(owner).__name__, "kind": kind, "request": request}
        )
        with self.sessions.write_unit_of_work() as sessions:
            context = TaskMutationContext(sessions, identity, current)
            with sessions.transaction() as session:
                receipt = session.get(TaskMutationReceiptRecord, identity)
                if receipt is not None:
                    if receipt.request_fingerprint != request_hash:
                        raise ValueError("같은 처리 식별자에 다른 입력을 사용할 수 없습니다.")
                    payload = json.loads(receipt.result_payload)
                    return self._replay(context, payload)
            scoped = owner._for_mutation(context)
            if request.get("task_id") is not None:
                context.watch(request["task_id"], request.get("occurrence_start"))
            result = operation(scoped)
            context.observe_result(result, kind)
            context.flush()
            payload = self._result(context, result)
            with sessions.transaction() as session:
                session.add(
                    TaskMutationReceiptRecord(
                        command_id=identity,
                        task_id=payload["task_id"],
                        request_fingerprint=request_hash,
                        command_kind=kind,
                        committed_at=current,
                        payload_version=1,
                        result_payload=encode(payload),
                    )
                )
        # Never signal success while SQLite can still roll back the outer UOW.
        if context.history_ids:
            self.sessions.task_events.publish(payload["task_id"])
        return result

    @staticmethod
    def _result(context: TaskMutationContext, result: Any) -> dict[str, Any]:
        values: dict[str, Any]
        if isinstance(result, Task):
            values = {"kind": "task", "task_id": result.id}
        elif isinstance(result, TaskOccurrence):
            values = {"kind": "occurrence", "task_id": result.task_id, "id": result.id}
        elif isinstance(result, ReminderDelivery):
            values = {"kind": "delivery", "task_id": result.task_id, "id": result.id}
        elif isinstance(result, str) and context.result_scope is not None:
            values = {
                "kind": "note",
                "task_id": context.result_scope[0],
                "occurrence_start": context.result_scope[1],
            }
        else:
            raise ValueError("지원하지 않는 업무 처리 결과입니다.")
        values["history_ids"] = context.history_ids
        return values

    @staticmethod
    def _replay(context: TaskMutationContext, payload: dict[str, Any]) -> Any:
        tasks = TaskService(context.task_repository, command_context=context)
        kind = payload["kind"]
        if kind == "task":
            return replace(tasks.get_including_deleted(payload["task_id"]), command_replayed=True)
        if kind == "occurrence":
            occurrence = tasks.occurrence_by_id(payload["id"])
            if occurrence is None:
                raise LookupError("이미 처리되었으나 회차를 찾을 수 없습니다.")
            return replace(occurrence, command_replayed=True)
        if kind == "delivery":
            delivery = context.reminder_repository.get_reminder_delivery(payload["id"])
            if delivery is None:
                raise LookupError("이미 처리되었으나 알림을 찾을 수 없습니다.")
            return replace(delivery, command_replayed=True)
        start = payload.get("occurrence_start")
        return ReplayedNote(
            tasks.result_note(payload["task_id"], datetime.fromisoformat(start) if start else None)
        )
