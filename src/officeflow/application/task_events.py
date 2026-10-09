import logging
from collections.abc import Callable
from threading import RLock


class TaskChangeEvents:
    """Instance-owned notifications emitted only after the outer DB commit."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._listeners: set[Callable[[int], None]] = set()

    def subscribe(self, callback: Callable[[int], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.add(callback)

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.discard(callback)

        return unsubscribe

    def publish(self, task_id: int) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for callback in listeners:
            try:
                callback(task_id)
            except Exception:
                logging.getLogger(__name__).exception("Task change notification failed")
