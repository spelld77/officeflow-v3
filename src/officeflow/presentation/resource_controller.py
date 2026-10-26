from __future__ import annotations

import json
import logging
import sys
from collections import deque
from dataclasses import asdict
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import monotonic
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from officeflow import __version__
from officeflow.application.resource_policy import ResourcePolicy, ResourceSnapshot, ResourceState
from officeflow.infrastructure.windows.resources import WindowsResourceProbe
from officeflow.presentation.latest_job import LatestJob


class _ResourceLogHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        raise OSError("resource_log_write") from None


class _Sampler:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.probe: WindowsResourceProbe | None = None
        self.handler: RotatingFileHandler | None = None
        self.policy = ResourcePolicy()
        self.run_id = uuid4().hex
        self.last_write = self.last_failure = -float("inf")

    def sample(self, metadata: dict[str, int | bool]) -> tuple[ResourceSnapshot, ResourceState]:
        try:
            if self.probe is None:
                self.probe = WindowsResourceProbe()
            snapshot = self.probe.sample()
        except (OSError, AttributeError, ValueError):
            snapshot = ResourceSnapshot(monotonic())
        previous = self.policy.state
        state = self.policy.observe(snapshot)
        interval = 300 if state is ResourceState.UNKNOWN else 60
        if previous != state or snapshot.sampled_at - self.last_write >= interval:
            self.write({**asdict(snapshot), **metadata, "state": state.value, "event": "sample"})
            self.last_write = snapshot.sampled_at
        return snapshot, state

    def write(self, values: dict[str, object]) -> None:
        if monotonic() - self.last_failure < 300:
            return
        try:
            if self.handler is None:
                self.handler = _ResourceLogHandler(
                    self.directory / "runtime-resources.log",
                    maxBytes=1024 * 1024,
                    backupCount=2,
                    encoding="utf-8",
                )
            payload = {
                "utc": datetime.now(UTC).isoformat(),
                "run": self.run_id,
                "version": __version__,
                "runtime": "exe" if getattr(sys, "frozen", False) else "python",
                **values,
            }
            self.handler.emit(
                logging.LogRecord(
                    "resources",
                    logging.INFO,
                    "",
                    0,
                    json.dumps(payload),
                    (),
                    None,
                )
            )
        except (OSError, ValueError):
            self.last_failure = monotonic()

    def close(self) -> None:
        self.write({"event": "shutdown"})
        if self.probe is not None:
            self.probe.close()
        if self.handler is not None:
            self.handler.close()
            self.handler = None


class ResourceController(QObject):
    stateChanged = Signal(object)

    def __init__(self, directory: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.samples: deque[ResourceSnapshot] = deque(maxlen=16)
        self.state = ResourceState.UNKNOWN
        self._sampler = _Sampler(directory)
        self._jobs = LatestJob(self)
        self._jobs.ready.connect(self._ready)
        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self.request)
        self._timer.start()
        self.metadata: dict[str, int | bool] = {}
        QTimer.singleShot(0, self.request)

    def request(self) -> None:
        if (
            self.state is ResourceState.PRESSURE
            and self.samples
            and monotonic() - self.samples[-1].sampled_at >= 120
        ):
            self.state = ResourceState.UNKNOWN
            self.stateChanged.emit(self.state)
        metadata = dict(self.metadata)
        self._jobs.submit(lambda: self._sampler.sample(metadata))

    def _ready(self, value: object) -> None:
        if not isinstance(value, tuple):
            return
        snapshot, state = value
        if monotonic() - snapshot.sampled_at >= 120:
            state = ResourceState.UNKNOWN
        self.samples.append(snapshot)
        if state != self.state:
            self.state = state
            self.stateChanged.emit(state)

    def shutdown(self) -> None:
        self._timer.stop()
        self._jobs.shutdown(self._sampler.close)
