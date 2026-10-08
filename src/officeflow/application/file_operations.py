"""Bounded heavy jobs and short file-use reservations (never a DB session)."""

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import PurePosixPath
from threading import RLock, Semaphore


class FileInUseError(RuntimeError):
    pass


class FileJobCanceledError(RuntimeError):
    pass


class FileOperationCoordinator:
    def __init__(self) -> None:
        self.gate = RLock()
        self._pins: Counter[str] = Counter()
        self._slot = Semaphore(1)

    @staticmethod
    def key(relative_path: str) -> str:
        return PurePosixPath(relative_path).as_posix().casefold()

    @contextmanager
    def pin(self, relative_path: str) -> Iterator[None]:
        key = self.key(relative_path)
        with self.gate:
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

    def assert_not_pinned(self, paths: tuple[str, ...]) -> None:
        with self.gate:
            if any(self._pins[self.key(path)] for path in paths):
                raise FileInUseError("파일을 확인/저장 중입니다. 작업이 끝난 뒤 다시 삭제하세요.")

    @contextmanager
    def job(self, cancel_requested: Callable[[], bool] | None = None) -> Iterator[None]:
        while not self._slot.acquire(timeout=0.1):
            if cancel_requested and cancel_requested():
                raise FileJobCanceledError("대기 중인 파일 작업을 취소했습니다.")
        try:
            if cancel_requested and cancel_requested():
                raise FileJobCanceledError("파일 작업을 취소했습니다.")
            yield
        finally:
            self._slot.release()
