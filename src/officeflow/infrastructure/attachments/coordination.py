"""Short, process-local gates for managed file publication and snapshot capture."""

from pathlib import Path
from threading import Lock, RLock

from officeflow.application.file_operations import FileOperationCoordinator

_registry_lock = Lock()
_coordinators: dict[Path, FileOperationCoordinator] = {}


def attachment_gate(root: Path) -> RLock:
    return attachment_coordinator(root).gate


def attachment_coordinator(root: Path) -> FileOperationCoordinator:
    key = root.resolve()
    with _registry_lock:
        return _coordinators.setdefault(key, FileOperationCoordinator())
