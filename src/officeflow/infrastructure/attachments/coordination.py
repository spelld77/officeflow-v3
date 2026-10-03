"""Short, process-local gates for managed file publication and snapshot capture."""

from pathlib import Path
from threading import Lock, RLock

_registry_lock = Lock()
_gates: dict[Path, RLock] = {}


def attachment_gate(root: Path) -> RLock:
    key = root.resolve()
    with _registry_lock:
        return _gates.setdefault(key, RLock())
