"""Explicit, shared command boundaries; never infer user actions from ORM flushes."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, ParamSpec, TypeVar, cast
from uuid import uuid4

from officeflow.domain.task_history import fingerprint

P = ParamSpec("P")
R = TypeVar("R")


class MutationAttempt:
    """One UI intent retains its ID until success; edited input is a new intent."""

    def __init__(self) -> None:
        self._request: str | None = None
        self._identity: str | None = None

    def identify(self, request: Any) -> str:
        signature = fingerprint(request)
        if signature != self._request or self._identity is None:
            self._request, self._identity = signature, uuid4().hex
        return self._identity

    def succeeded(self) -> None:
        self._request, self._identity = None, None


def tracked_command(method: Callable[P, R]) -> Callable[P, R]:  # noqa: UP047
    signature = inspect.signature(method)

    @wraps(method)
    def execute(*args: P.args, **kwargs: P.kwargs) -> R:
        bound = signature.bind(*args, **kwargs)
        owner: Any = bound.arguments["self"]
        arguments = {key: value for key, value in bound.arguments.items() if key != "self"}
        context = getattr(owner, "_command_context", None)
        if context is not None:
            task_id = arguments.get("task_id")
            if task_id is not None:
                context.watch(task_id, arguments.get("occurrence_start"))
            result = method(*args, **kwargs)
            context.observe_result(result, method.__name__)
            return result
        runner = getattr(owner, "_mutation_runner", None)
        if runner is None:  # In-memory application ports retain their old contract.
            return method(*args, **kwargs)
        current = arguments.get("now") or datetime.now(UTC)
        arguments["now"] = current
        command_id = arguments.pop("command_id", None)
        request = {key: value for key, value in arguments.items() if key != "now"}
        return cast(
            R,
            runner.execute(
                owner,
                method.__name__,
                request,
                current,
                command_id,
                lambda scoped: method(scoped, **arguments),
            ),
        )

    return execute
