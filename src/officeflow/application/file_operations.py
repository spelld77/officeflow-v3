"""Bounded heavy jobs and short file-use reservations (never a DB session)."""

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import PurePosixPath
from threading import RLock, Semaphore, local


class FileInUseError(RuntimeError):
    pass


class FileJobCanceledError(RuntimeError):
    pass


class FileOperationCoordinator:
    def __init__(self) -> None:
        self.gate = RLock()
        self._pins: Counter[str] = Counter()
        self._deleting: dict[str, str] = {}
        self._protected: dict[str, frozenset[str] | None] = {}
        self._slot = Semaphore(1)
        self._foreground_waiters = 0
        self._job_context = local()

    @staticmethod
    def key(relative_path: str) -> str:
        return PurePosixPath(relative_path).as_posix().casefold()

    @contextmanager
    def pin(self, relative_path: str, *, owner: str | None = None) -> Iterator[None]:
        key = self.key(relative_path)
        with self.gate:
            self.assert_accessible(relative_path, owner=owner)
            self._pins[key] += 1
        try:
            yield
        finally:
            with self.gate:
                self._pins[key] -= 1
                if not self._pins[key]:
                    del self._pins[key]

    def is_pinned(self, relative_path: str) -> bool:
        with self.gate:
            return bool(self._pins[self.key(relative_path)])

    @contextmanager
    def preservation_pin(self, relative_path: str) -> Iterator[None]:
        """Read-only full-backup reservation, including protected recovery materials.

        Caller owns the heavy-job slot. This cannot authorize mutation or an editor.
        """
        key = self.key(relative_path)
        with self.gate:
            if key in self._deleting:
                raise FileInUseError("삭제 명령이 진행 중입니다. 백업을 다시 시도하세요.")
            self._pins[key] += 1
        try:
            yield
        finally:
            with self.gate:
                self._pins[key] -= 1
                if not self._pins[key]:
                    del self._pins[key]

    def assert_not_pinned(self, paths: tuple[str, ...]) -> None:
        with self.gate:
            for path in paths:
                self.assert_accessible(path)
            if any(self._pins[self.key(path)] for path in paths):
                raise FileInUseError("파일을 확인/저장 중입니다. 작업이 끝난 뒤 다시 삭제하세요.")

    def protect(self, owner: str, paths: tuple[str, ...] | None) -> None:
        """Restart reservations survive failed recovery; None blocks file I/O only."""
        with self.gate:
            self._protected[owner] = (
                frozenset(self.key(path) for path in paths) if paths is not None else None
            )

    def unprotect(self, owner: str) -> None:
        with self.gate:
            self._protected.pop(owner, None)

    def is_protected(self, relative_path: str) -> bool:
        with self.gate:
            key = self.key(relative_path)
            return key in self._deleting or any(
                paths is None or key in paths for paths in self._protected.values()
            )

    def assert_accessible(self, relative_path: str, *, owner: str | None = None) -> None:
        with self.gate:
            key = self.key(relative_path)
            if key in self._deleting and self._deleting[key] != owner:
                raise FileInUseError("삭제 처리 중인 파일입니다. 잠시 후 다시 시도하세요.")
            if any(
                other != owner and (paths is None or key in paths)
                for other, paths in self._protected.items()
            ):
                raise FileInUseError(
                    "중단된 파일 작업 확인이 필요합니다. 자료를 보존하고 있습니다."
                )

    @contextmanager
    def deletion_intent(self, paths: tuple[str, ...], *, owner: str) -> Iterator[None]:
        """An exclusive intent rejects new readers, rather than waiting on the UI."""
        keys = {self.key(path) for path in paths}
        with self.gate:
            self.assert_not_pinned(paths)
            self._deleting.update(dict.fromkeys(keys, owner))
        try:
            yield
        finally:
            with self.gate:
                for key in keys:
                    self._deleting.pop(key, None)

    @contextmanager
    def job(
        self, cancel_requested: Callable[[], bool] | None = None, *, background: bool = False
    ) -> Iterator[None]:
        from threading import Event

        if getattr(self._job_context, "active", False):
            if cancel_requested and cancel_requested():
                raise FileJobCanceledError("파일 작업을 취소했습니다.")
            yield  # same worker's nested capture is part of the already owned job
            return

        if not background:
            with self.gate:
                self._foreground_waiters += 1
        try:
            while True:
                if cancel_requested and cancel_requested():
                    raise FileJobCanceledError("대기 중인 파일 작업을 취소했습니다.")
                with self.gate:
                    defer = background and self._foreground_waiters > 0
                if defer:
                    Event().wait(0.05)
                    continue
                if self._slot.acquire(timeout=0.05):
                    break
        finally:
            if not background:
                with self.gate:
                    self._foreground_waiters -= 1
        try:
            if cancel_requested and cancel_requested():
                raise FileJobCanceledError("파일 작업을 취소했습니다.")
            self._job_context.active = True
            yield
        finally:
            self._job_context.active = False
            self._slot.release()
