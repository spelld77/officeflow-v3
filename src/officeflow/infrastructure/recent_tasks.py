"""Disposable local history, excluded from business backups and settings."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from officeflow.application.recent_tasks import RecentTaskRef


def _aware(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("date")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timezone")
    return result.astimezone(UTC)


class RecentTaskStore:
    MAX_BYTES = 32768

    def __init__(self, path: Path, database: Path) -> None:
        self.path, self.database = path, database
        self._lock = RLock()
        self._revision = 0
        self.identity = self._identity()

    def _identity(self) -> str | None:
        try:
            stat = self.database.stat()
            return (
                f"{stat.st_dev}:{stat.st_ino}"
                if stat.st_ino and not self.database.is_symlink()
                else None
            )
        except OSError:
            return None

    def _safe(self) -> bool:
        return (
            not self.path.is_symlink()
            and not self.path.is_junction()
            and self.path.parent.resolve() == self.path.parent.absolute()
            and not self.path.parent.is_junction()
        )

    def load(self) -> tuple[RecentTaskRef, ...]:
        if not self.identity or not self._safe() or self._identity() != self.identity:
            return ()
        try:
            with self.path.open("rb") as stream:
                payload = stream.read(self.MAX_BYTES + 1)
            if len(payload) > self.MAX_BYTES:
                return ()
            value = json.loads(payload)
            if (
                not isinstance(value, dict)
                or value.get("version") != 1
                or value.get("database_file_identity") != self.identity
                or not isinstance(value.get("entries"), list)
            ):
                return ()
            result: list[RecentTaskRef] = []
            seen: set[int] = set()
            for row in value["entries"][:100]:
                try:
                    if (
                        not isinstance(row, dict)
                        or type(row.get("task_id")) is not int
                        or row["task_id"] <= 0
                    ):
                        continue
                    ref = RecentTaskRef(
                        row["task_id"],
                        _aware(row.get("task_created_at")),
                        _aware(row["occurrence_start"])
                        if row.get("occurrence_start") is not None
                        else None,
                        _aware(row.get("viewed_at")),
                    )
                    if ref.task_id not in seen:
                        result.append(ref)
                        seen.add(ref.task_id)
                    if len(result) == 20:
                        break
                except (ValueError, TypeError, KeyError):
                    continue
            return tuple(result)
        except (OSError, ValueError, UnicodeError):
            return ()

    def reserve(self) -> int:
        with self._lock:
            self._revision += 1
            return self._revision

    def save(self, refs: tuple[RecentTaskRef, ...], revision: int) -> None:
        if not self.identity:
            return
        if not self._safe() or self._identity() != self.identity:
            raise OSError("recent_store_unavailable")
        rows = [
            {
                k: v.astimezone(UTC).isoformat() if isinstance(v, datetime) else v
                for k, v in asdict(r).items()
            }
            for r in refs[:20]
        ]
        payload = json.dumps(
            {"version": 1, "database_file_identity": self.identity, "entries": rows}
        ).encode("utf-8")
        if len(payload) > self.MAX_BYTES:
            raise ValueError("recent_store_size")
        name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.path.parent, prefix=".recent-", delete=False
            ) as stream:
                name = stream.name
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            with self._lock:
                if revision == self._revision and self._safe():
                    os.replace(name, self.path)
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)

    def invalidate(self) -> bool:
        self.reserve()
        try:
            if not self._safe():
                return False
            self.path.unlink(missing_ok=True)
            return True
        except OSError:
            return False
