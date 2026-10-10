"""Local attempt metadata; never part of a restored business-data backup."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BackupAttempt:
    reason: str
    status: str
    attempted_at: str
    finished_at: str = ""
    last_success_at: str = ""
    error: str = ""
    acknowledged: bool = False
    last_failure_at: str = ""
    last_failure_error: str = ""
    unresolved_failure: bool = False


@dataclass(frozen=True, slots=True)
class BackupHealth:
    attempts: tuple[BackupAttempt, ...]
    attention: bool
    recording_warning: str = ""


def backup_error_summary(error: object) -> str:
    """Do not persist arbitrary exception messages containing document/user data."""
    if isinstance(error, PermissionError):
        return "파일 접근 권한 또는 보안 프로그램의 차단을 확인하세요."
    if isinstance(error, OSError) and error.errno == 28:
        return "백업 작업 공간이 부족합니다. 저장 공간을 확인하세요."
    if type(error).__name__ == "BackupSourceIncompleteError":
        return "필요한 첨부파일이 누락되었습니다. 자료 검사에서 확인하세요."
    return "백업을 완료하지 못했습니다. 자료 검사와 저장 공간·파일 접근 상태를 확인하세요."


class BackupHealthStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()
        self._active: set[str] = set()
        self._warning = ""
        self._rows: dict[str, BackupAttempt] = {}
        try:
            if path.exists():
                if path.stat().st_size > 64 * 1024:
                    raise ValueError("oversize")
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload["version"] != 1:
                    raise ValueError("version")
                for raw in payload["attempts"]:
                    row = BackupAttempt(**raw)
                    if row.reason not in {"manual", "automatic"} or row.status not in {
                        "running",
                        "success",
                        "failed",
                        "canceled",
                    }:
                        raise ValueError("status")
                    if (
                        not all(
                            isinstance(value, str)
                            for key, value in raw.items()
                            if key not in {"acknowledged", "unresolved_failure"}
                        )
                        or type(row.acknowledged) is not bool
                        or type(row.unresolved_failure) is not bool
                    ):
                        raise ValueError("types")
                    for stamp in (
                        row.attempted_at,
                        row.finished_at,
                        row.last_success_at,
                        row.last_failure_at,
                    ):
                        if stamp and datetime.fromisoformat(stamp).tzinfo is None:
                            raise ValueError("time")
                    self._rows[row.reason] = row
        except (OSError, ValueError, KeyError, TypeError):
            self._rows.clear()
            self._warning = "백업 상태 기록을 읽지 못했습니다. 실제 백업 파일을 확인하세요."

    def snapshot(self) -> BackupHealth:
        with self._lock:
            rows = tuple(self._rows[key] for key in sorted(self._rows))
            attention = bool(self._warning) or any(
                not row.acknowledged
                and (
                    row.unresolved_failure
                    or row.status == "failed"
                    or (row.status == "running" and row.reason not in self._active)
                )
                for row in rows
            )
            # Persisted running state from an earlier process is not a success.
            rows = tuple(
                replace(row, status="unconfirmed")
                if row.status == "running" and row.reason not in self._active
                else row
                for row in rows
            )
            return BackupHealth(rows, attention, self._warning)

    def begin(self, reason: str) -> None:
        if reason not in {"manual", "automatic"}:
            return
        with self._lock:
            previous = self._rows.get(reason)
            self._rows[reason] = BackupAttempt(
                reason,
                "running",
                datetime.now(UTC).isoformat(),
                last_success_at=previous.last_success_at if previous else "",
                last_failure_at=previous.last_failure_at if previous else "",
                last_failure_error=previous.last_failure_error if previous else "",
                unresolved_failure=previous.unresolved_failure if previous else False,
            )
            self._active.add(reason)
            self._flush()

    def finish(self, reason: str, status: str, error: str = "") -> None:
        if reason not in {"manual", "automatic"}:
            return
        with self._lock:
            previous = self._rows.get(reason)
            now = datetime.now(UTC).isoformat()
            self._rows[reason] = BackupAttempt(
                reason,
                status,
                previous.attempted_at if previous else now,
                now,
                now if status == "success" else previous.last_success_at if previous else "",
                error,
                last_failure_at=now
                if status == "failed"
                else previous.last_failure_at
                if previous
                else "",
                last_failure_error=error
                if status == "failed"
                else previous.last_failure_error
                if previous
                else "",
                unresolved_failure=status == "failed"
                or (status == "success" and bool(error))
                or (status != "success" and bool(previous and previous.unresolved_failure)),
            )
            self._active.discard(reason)
            self._flush()

    def waiting_canceled(self, reason: str) -> None:
        with self._lock:
            # A waiting dialog must not overwrite another dialog's active attempt.
            if reason not in self._active:
                self.begin(reason)
                self.finish(reason, "canceled")

    def acknowledge(self) -> None:
        with self._lock:
            self._rows = {
                key: replace(row, acknowledged=True, unresolved_failure=False)
                for key, row in self._rows.items()
            }
            self._flush()

    def _flush(self) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.part")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(
                    {"version": 1, "attempts": [asdict(row) for row in self._rows.values()]}, stream
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._warning = ""
        except OSError as error:
            self._warning = "백업 상태 기록을 저장하지 못했습니다. 실제 백업 파일을 확인하세요."
            logger.error("Backup health persistence failed (%s)", type(error).__name__)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning("Backup health temporary metadata could not be removed")
