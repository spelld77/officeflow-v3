"""Best-effort in-process diagnostics, not a watchdog or security-policy bypass."""

from __future__ import annotations

import faulthandler
import logging
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

logger = logging.getLogger(__name__)


class RuntimeDiagnostics:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.notify: Callable[[str], None] | None = None
        self._previous = sys.excepthook
        self._hook = self._exception
        self._previous_thread = threading.excepthook
        self._thread_hook = self._thread_exception
        self._stream: BinaryIO | None = None
        self._owns_fault_handler = False

    def install(self) -> None:
        if sys.excepthook == self._hook:
            return
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            # Do not replace a handler installed by a test host or embedding app.
            if not faulthandler.is_enabled():
                current = self.log_dir / "native-fault.log"
                # Normal restarts must not rotate empty files over meaningful faults.
                if current.exists() and current.stat().st_size:
                    for index in (2, 1):
                        source = self.log_dir / f"native-fault.log{'' if index == 1 else '.1'}"
                        target = self.log_dir / f"native-fault.log.{index}"
                        if source.exists():
                            source.replace(target)
                self._stream = (self.log_dir / "native-fault.log").open("wb")
                faulthandler.enable(self._stream, all_threads=True)
                self._owns_fault_handler = True
        except (OSError, RuntimeError) as error:
            logger.warning("Native diagnostic recorder unavailable (%s)", type(error).__name__)
            if self._stream:
                self._stream.close()
                self._stream = None
        self._previous = sys.excepthook
        sys.excepthook = self._hook
        self._previous_thread = threading.excepthook
        threading.excepthook = self._thread_hook

    def _thread_exception(self, args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        self._exception(args.exc_type, args.exc_value, args.exc_traceback)

    def _exception(
        self, kind: type[BaseException], value: BaseException | None, tb: TracebackType | None
    ) -> None:
        if issubclass(kind, KeyboardInterrupt) and value is not None:
            self._previous(kind, value, tb)
            return
        # No str(value), source-line text, locals, task fields or document bytes.
        frames = " > ".join(
            f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
            for frame in traceback.extract_tb(tb)
        )
        logger.error("Unhandled Python exception %s at %s", kind.__name__, frames)
        if self.notify:
            try:
                self.notify(
                    "예기치 않은 오류가 발생했습니다. 데이터 → 자료 검사에서 진단 폴더를 확인하세요."
                )
            except Exception as error:
                logger.error("Diagnostic notification failed (%s)", type(error).__name__)

    def close(self) -> None:
        self.notify = None
        if sys.excepthook == self._hook:
            sys.excepthook = self._previous
        if threading.excepthook == self._thread_hook:
            threading.excepthook = self._previous_thread
        if self._owns_fault_handler:
            faulthandler.disable()
            self._owns_fault_handler = False
        if self._stream:
            self._stream.close()
            self._stream = None
