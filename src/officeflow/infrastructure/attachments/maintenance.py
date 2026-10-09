"""Bounded, yielding recovery scans. A failed first job must not starve later jobs."""

from __future__ import annotations

import heapq
import os
from collections.abc import Callable
from time import monotonic, time
from typing import Any

from officeflow.application.attachment_deletion import (
    AttachmentDeletionService,
    DeleteRecoveryItem,
    DeleteRecoveryReport,
)
from officeflow.application.attachment_documents import (
    AttachmentDocumentService,
    InternalRecoveryItem,
    InternalRecoveryReport,
)
from officeflow.infrastructure.attachments.deletion_storage import DeletionFileStorage
from officeflow.infrastructure.attachments.internal_storage import (
    InternalFileStorage,
    _decode_command,
)
from officeflow.infrastructure.attachments.view_cache import AttachmentViewCache


class FileMaintenance:
    def __init__(
        self,
        documents: AttachmentDocumentService,
        deletion: AttachmentDeletionService,
        internal: InternalFileStorage,
        trash: DeletionFileStorage,
        cache: AttachmentViewCache,
    ) -> None:
        self.documents, self.deletion, self.internal, self.trash, self.cache = (
            documents,
            deletion,
            internal,
            trash,
            cache,
        )
        self.scans: list[tuple[str, Any]] = []
        self._next_scan = 0.0
        self._scan()
        self.queue: list[tuple[float, str, str]] = []
        self.known: set[tuple[str, str]] = set()
        self.warnings: dict[str, str] = {}
        self._reported: tuple[str, ...] = ()
        self.snapshots: Any = None

    def _scan(self) -> None:
        for kind, path in (
            ("internal", self.internal.root / ".operations"),
            ("delete", self.cache.root / ".trash"),
        ):
            if path.is_dir():
                self.scans.append((kind, os.scandir(path)))
        self._next_scan = time() + 60

    def tick(self, canceled: Callable[[], bool]) -> InternalRecoveryReport:
        start, count, recovered = monotonic(), 0, 0
        if not self.scans and time() >= self._next_scan:
            self._scan()
        while self.scans and count < 20 and monotonic() - start < 2 and not canceled():
            kind, scan = self.scans[0]
            try:
                entry = next(scan)
            except StopIteration:
                scan.close()
                self.scans.pop(0)
                continue
            count += 1
            if kind == "internal" and entry.name.startswith("snapshot-"):
                kind = "snapshot"
            if kind == "delete" and not entry.name.startswith("delete-"):
                continue
            identity = (
                entry.name.removeprefix("delete-")
                if kind == "delete"
                else entry.name.removeprefix("snapshot-")
                if kind == "snapshot"
                else entry.name
            )
            if (kind, identity) in self.known:
                continue
            self.known.add((kind, identity))
            heapq.heappush(self.queue, (time(), kind, identity))
            try:
                (
                    self.internal
                    if kind == "internal"
                    else self.internal.snapshot_storage
                    if kind == "snapshot"
                    else self.trash
                ).protect_pending(identity)
            except Exception as error:
                self.warnings[kind + identity] = str(error)
        if self.scans:
            return InternalRecoveryReport(0, len(self.queue), ())
        self.documents.coordinator.unprotect("internal:overflow")
        self.documents.coordinator.unprotect("delete:overflow")
        # One job per tick releases the heavy slot before the next candidate.
        if self.queue and self.queue[0][0] <= time() and not canceled():
            _due, kind, identity = heapq.heappop(self.queue)
            try:
                report: InternalRecoveryReport | DeleteRecoveryReport
                if kind == "internal":
                    payload = self.internal._read(identity)
                    item = InternalRecoveryItem(identity, "", _decode_command(payload["command"]))
                    report = self.documents.recover_confirmed(
                        (item,), cancel_requested=canceled, background=True
                    )
                elif kind == "snapshot":
                    if self.snapshots is None:
                        raise ValueError("중요 보관 복구 서비스를 확인하세요.")
                    report = self.snapshots.recover(identity, canceled, background=True)
                else:
                    command, _entries = self.trash._read(identity, with_states=False)
                    report = self.deletion.recover(
                        (DeleteRecoveryItem(identity, command, ""),),
                        cancel_requested=canceled,
                        background=True,
                    )
                recovered = report.recovered
                if report.pending:
                    heapq.heappush(self.queue, (time() + 3600, kind, identity))
                else:
                    self.known.discard((kind, identity))
                self.warnings[kind + identity] = " · ".join(report.warnings)
            except Exception as error:
                self.warnings[kind + identity] = str(error)
                heapq.heappush(self.queue, (time() + 3600, kind, identity))
        self.cache.maintenance(canceled)
        warnings = tuple(value for value in self.warnings.values() if value)
        changed = warnings != self._reported
        self._reported = warnings
        return InternalRecoveryReport(recovered, len(self.queue), warnings if changed else ())

    def close(self) -> None:
        for _kind, scan in self.scans:
            scan.close()
        self.scans.clear()
