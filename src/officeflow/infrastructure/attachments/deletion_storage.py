"""Exact per-job quarantine; handle-based no-overwrite moves and deletions."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from time import monotonic
from uuid import uuid4

from officeflow.application.attachment_deletion import (
    DeleteAsset,
    DeleteCommand,
    DeleteCommit,
    DeletePlan,
    DeleteRecoveryItem,
    DeleteRepository,
    decode_delete_target,
)
from officeflow.application.attachments import AttachmentOperationError
from officeflow.application.file_operations import FileJobCanceledError, FileOperationCoordinator
from officeflow.infrastructure.attachments.internal_storage import checked_managed_path
from officeflow.infrastructure.attachments.read_lease import (
    delete_open_file,
    exclusive_read,
    file_identity,
    rename_open_file,
)

_MAX_JOURNAL = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _Entry:
    asset: DeleteAsset
    state: str = "initial"
    identity: tuple[int, int, int, int] | None = None


class DeletionFileStorage:
    def __init__(
        self,
        root: Path,
        *,
        coordinator: FileOperationCoordinator,
        checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self._root = root.resolve()
        self.coordinator = coordinator
        self._checkpoint = checkpoint or (lambda _stage: None)

    def checkpoint(self, stage: str) -> None:
        self._checkpoint(stage)

    @staticmethod
    def _owner(job_id: str) -> str:
        return "delete:" + job_id

    @staticmethod
    def _folder(job_id: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("삭제 작업 ID가 올바르지 않습니다.")
        return ".trash/delete-" + job_id

    def _path(self, relative: str, *, required: bool = False) -> Path:
        return checked_managed_path(self._root, relative, file_required=required)

    def _quarantine(self, job_id: str, asset: DeleteAsset) -> str:
        return self._folder(job_id) + "/" + asset.id

    def _journal(self, job_id: str) -> Path:
        return self._path(self._folder(job_id) + "/delete-plan.json")

    def _write(self, command: DeleteCommand, entries: tuple[_Entry, ...]) -> None:
        target = self._journal(command.job_id)
        raw = json.dumps(
            {
                "version": 2,
                "command": asdict(command),
                "entries": [asdict(entry) for entry in entries],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        if len(raw) > _MAX_JOURNAL:
            raise AttachmentOperationError(
                "삭제 범위가 저널 안전 한도를 초과했습니다. 자료를 나눠 정리해 주세요."
            )
        temporary = self._path(self._folder(command.job_id) + "/journal-" + uuid4().hex + ".tmp")
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)

    def _entry_path(self, job_id: str, asset_id: str) -> Path:
        return self._path(self._folder(job_id) + "/" + asset_id + ".state.json")

    def _write_entry(self, command: DeleteCommand, entry: _Entry) -> None:
        # One small durable file per asset: never rewrite an N-file plan N times.
        target = self._entry_path(command.job_id, entry.asset.id)
        raw: dict[str, object] = {
            "request": command.request_fingerprint,
            "asset": entry.asset.id,
            "state": entry.state,
            "identity": entry.identity,
        }
        self._write_small(target, raw)

    def _write_small(self, target: Path, raw: dict[str, object]) -> None:
        temporary = target.with_name("journal-" + uuid4().hex + ".tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(raw, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)

    def _read_entry(self, command: DeleteCommand, entry: _Entry) -> _Entry:
        path = self._entry_path(command.job_id, entry.asset.id)
        if not path.exists():
            return entry
        self._path(path.relative_to(self._root).as_posix(), required=True)
        if path.stat().st_size > 2048:
            raise ValueError("삭제 파일 상태 기록이 너무 큽니다.")
        raw = self._small_json(path)
        if (
            not isinstance(raw, dict)
            or set(raw) != {"request", "asset", "state", "identity"}
            or raw["request"] != command.request_fingerprint
            or raw["asset"] != entry.asset.id
        ):
            raise ValueError("삭제 파일 상태 기록이 일치하지 않습니다.")
        identity = raw["identity"]
        if (
            raw["state"] not in {"prepared", "missing"}
            or (raw["state"] == "prepared") != (identity is not None)
            or not entry.asset.removable
        ):
            raise ValueError("삭제 파일의 준비 기록이 올바르지 않습니다.")
        if identity is not None and (
            not isinstance(identity, list)
            or len(identity) != 4
            or any(type(part) is not int or part < 0 for part in identity)
        ):
            raise ValueError("삭제 파일의 identity 기록이 올바르지 않습니다.")
        return replace(
            entry, state=raw["state"], identity=tuple(identity) if identity is not None else None
        )

    @staticmethod
    def _small_json(path: Path) -> object:
        with path.open("rb") as stream:
            raw = stream.read(2049)
        if len(raw) > 2048:
            raise ValueError("삭제 상태 기록의 크기가 변경되었습니다.")
        return json.loads(raw)

    def _read(
        self, job_id: str, *, with_states: bool = True
    ) -> tuple[DeleteCommand, tuple[_Entry, ...]]:
        journal = self._path(self._folder(job_id) + "/delete-plan.json", required=True)
        if journal.stat().st_size > _MAX_JOURNAL:
            raise ValueError("삭제 저널이 안전 한도를 초과했습니다.")
        with journal.open("rb") as stream:
            data = stream.read(_MAX_JOURNAL + 1)
        if len(data) > _MAX_JOURNAL:
            raise ValueError("삭제 저널 크기가 변경되었습니다.")
        raw = json.loads(data)
        if (
            not isinstance(raw, dict)
            or set(raw) != {"version", "command", "entries"}
            or type(raw["version"]) is not int
            or raw["version"] != 2
        ):
            raise ValueError("지원하지 않는 삭제 저널입니다.")
        request = raw["command"]
        if not isinstance(request, dict) or set(request) != {
            "job_id",
            "target",
            "plan_fingerprint",
            "include_versions",
        }:
            raise ValueError("삭제 명령 형식이 올바르지 않습니다.")
        command = DeleteCommand(
            request["job_id"],
            decode_delete_target(request["target"]),
            request["plan_fingerprint"],
            request["include_versions"],
        )
        if command.job_id != job_id or not isinstance(raw["entries"], list):
            raise ValueError("삭제 작업의 소유 정보가 일치하지 않습니다.")
        entries: list[_Entry] = []
        for value in raw["entries"]:
            if (
                not isinstance(value, dict)
                or set(value) != {"asset", "state", "identity"}
                or not isinstance(value["asset"], dict)
                or set(value["asset"]) != {"id", "path", "kind", "removable"}
            ):
                raise ValueError("삭제 파일 형식이 올바르지 않습니다.")
            asset = DeleteAsset(**value["asset"])
            if asset.path.split("/")[0].startswith("."):
                raise ValueError("작업/격리 영역은 삭제 출처가 될 수 없습니다.")
            pure = PurePosixPath(asset.path)
            if (
                pure.is_absolute()
                or "\\" in asset.path
                or ":" in asset.path
                or ".." in pure.parts
                or pure.as_posix() != asset.path
                or any(part.endswith((" ", ".")) for part in pure.parts)
            ):
                raise ValueError("삭제 경로가 올바르지 않습니다.")
            if with_states:
                self._path(asset.path)
            identity = value["identity"]
            if identity is not None and (
                not isinstance(identity, list)
                or len(identity) != 4
                or any(type(part) is not int or part < 0 for part in identity)
            ):
                raise ValueError("삭제 파일 identity가 올바르지 않습니다.")
            state = value["state"]
            if (
                state not in {"initial", "prepared", "missing", "kept"}
                or (state == "prepared") != (identity is not None)
                or (not asset.removable and state != "kept")
                or (asset.removable and state == "kept")
            ):
                raise ValueError("삭제 파일 상태가 올바르지 않습니다.")
            entries.append(_Entry(asset, state, tuple(identity) if identity is not None else None))
        if len({entry.asset.id for entry in entries}) != len(entries) or len(
            {entry.asset.path.casefold() for entry in entries}
        ) != len(entries):
            raise ValueError("삭제 파일이 중복되었습니다.")
        return command, tuple(
            self._read_entry(command, entry) for entry in entries
        ) if with_states else tuple(entries)

    def prepare(
        self, command: DeleteCommand, plan: DeletePlan, canceled: Callable[[], bool]
    ) -> None:
        folder = self._path(self._folder(command.job_id))
        folder.parent.mkdir(parents=True, exist_ok=True)
        self._path(self._folder(command.job_id))
        folder.mkdir(exist_ok=False)
        entries = tuple(
            _Entry(asset, "initial" if asset.removable else "kept") for asset in plan.assets
        )
        self._write(command, entries)
        self.checkpoint("journal")
        for entry in entries:
            if canceled():
                raise FileJobCanceledError("파일 격리를 취소했습니다.")
            if not entry.asset.removable:
                continue
            source = self._path(entry.asset.path)
            if not source.exists():
                self._write_entry(command, replace(entry, state="missing"))
                continue
            source = self._path(entry.asset.path, required=True)
            with exclusive_read(source) as stream:
                identity = file_identity(os.fstat(stream.fileno()))
                if identity != file_identity(source.stat()):
                    raise AttachmentOperationError("격리할 파일이 변경되었습니다.")
                prepared = replace(entry, state="prepared", identity=identity)
                self._write_entry(command, prepared)  # Durable identity BEFORE the actual move.
                self.checkpoint("prepared")
                with self.coordinator.gate:
                    destination = self._path(self._quarantine(command.job_id, entry.asset))
                    rename_open_file(stream, destination)
                self.checkpoint("moved")

    def assert_quarantined(self, command: DeleteCommand, assets: tuple[DeleteAsset, ...]) -> None:
        actual, entries = self._read(command.job_id)
        if actual != command or tuple(entry.asset for entry in entries) != assets:
            raise AttachmentOperationError("삭제 계획과 격리 저널이 일치하지 않습니다.")
        for entry in entries:
            if not entry.asset.removable:
                continue
            original = self._path(entry.asset.path)
            quarantine = self._path(self._quarantine(command.job_id, entry.asset))
            if original.exists() or entry.state not in {"prepared", "missing"}:
                raise AttachmentOperationError("격리 상태가 변경되어 삭제를 확정하지 않았습니다.")
            if entry.state == "prepared" and (
                not quarantine.is_file()
                or file_identity(
                    self._path(self._quarantine(command.job_id, entry.asset), required=True).stat()
                )
                != entry.identity
            ):
                raise AttachmentOperationError("격리 파일의 identity가 변경되었습니다.")
            if entry.state == "missing" and quarantine.exists():
                raise AttachmentOperationError("누락 파일의 격리 경로에 다른 자료가 있습니다.")

    @contextmanager
    def commit_guard(
        self, command: DeleteCommand, assets: tuple[DeleteAsset, ...]
    ) -> Iterator[None]:
        """Keep exact quarantine handles across the short DB commit, not just stat."""
        actual, entries = self._read(command.job_id)
        if actual != command or tuple(entry.asset for entry in entries) != assets:
            raise AttachmentOperationError("삭제 계획과 격리 저널이 일치하지 않습니다.")
        with ExitStack() as handles:
            for entry in entries:
                if entry.asset.removable and entry.state == "prepared":
                    stream = handles.enter_context(
                        exclusive_read(
                            self._path(self._quarantine(command.job_id, entry.asset), required=True)
                        )
                    )
                    if file_identity(os.fstat(stream.fileno())) != entry.identity:
                        raise AttachmentOperationError("삭제 확정 전 격리 파일이 변경되었습니다.")
            self.assert_quarantined(command, assets)
            self.checkpoint("commit_guard")
            yield

    def _finished_mode(self, command: DeleteCommand) -> str | None:
        path = self._path(self._folder(command.job_id) + "/finished.json")
        if not path.exists():
            return None
        self._path(path.relative_to(self._root).as_posix(), required=True)
        if path.stat().st_size > 2048:
            raise ValueError("삭제 마무리 기록이 너무 큽니다.")
        raw = self._small_json(path)
        if (
            not isinstance(raw, dict)
            or set(raw) != {"request", "mode"}
            or raw["request"] != command.request_fingerprint
            or raw["mode"] not in {"deleted", "rolled_back"}
        ):
            raise ValueError("삭제 마무리 기록이 일치하지 않습니다.")
        return str(raw["mode"])

    def _finish_journal(self, command: DeleteCommand, mode: str) -> None:
        folder = self._path(self._folder(command.job_id))
        # Unknown files / editor sidecars / interrupted JSON writes are never removed.
        actual, entries = self._read(command.job_id)
        allowed = {"delete-plan.json", "finished.json"} | {
            entry.asset.id + ".state.json" for entry in entries
        }
        if actual != command or any(child.name not in allowed for child in folder.iterdir()):
            raise AttachmentOperationError("삭제 작업 폴더에 확인하지 않은 자료가 남아 있습니다.")
        # Persist a completion marker before removing per-file state records;
        # restart during metadata cleanup must not mistake them for unprepared files.
        prior = self._finished_mode(command)
        if prior is not None and prior != mode:
            raise AttachmentOperationError("삭제 마무리 종류가 일치하지 않습니다.")
        if prior is None:
            self._write_small(
                self._path(self._folder(command.job_id) + "/finished.json"),
                {"request": command.request_fingerprint, "mode": mode},
            )
        for entry in entries:
            self._entry_path(command.job_id, entry.asset.id).unlink(missing_ok=True)
            self.checkpoint("state_removed")
        # Keep the plan until after the small records. If removing either final
        # metadata file is interrupted, a prefix-owned folder stays fail-closed.
        self._path(self._folder(command.job_id) + "/finished.json").unlink()
        self._journal(command.job_id).unlink()
        folder.rmdir()
        self.coordinator.unprotect(self._owner(command.job_id))

    def rollback(
        self,
        command: DeleteCommand,
        repository: DeleteRepository,
        *,
        canceled: Callable[[], bool] = lambda: False,
    ) -> None:
        folder = self._path(self._folder(command.job_id))
        if not folder.exists():
            self.coordinator.unprotect(self._owner(command.job_id))
            return
        actual, entries = self._read(command.job_id)
        if actual != command or repository.committed(command) is not None:
            raise AttachmentOperationError("삭제 미확정 상태를 확인할 수 없습니다.")
        completed = self._finished_mode(command)
        if completed is not None:
            if completed != "rolled_back" or any(
                repository.recovery_state(entry.asset) != "live"
                or self._path(self._quarantine(command.job_id, entry.asset)).exists()
                for entry in entries
            ):
                raise AttachmentOperationError("복구 완료 기록을 확인할 수 없습니다.")
            self._finish_journal(command, "rolled_back")
            return
        for entry in reversed(entries):
            if canceled():
                raise AttachmentOperationError(
                    "복구 정리를 중단했습니다. 격리 자료는 다음 점검까지 보존합니다."
                )
            with self.coordinator.gate:
                self.coordinator.assert_accessible(
                    entry.asset.path, owner=self._owner(command.job_id)
                )
                if self.coordinator.is_pinned(entry.asset.path) or self.coordinator.is_pinned(
                    self._quarantine(command.job_id, entry.asset)
                ):
                    raise AttachmentOperationError(
                        "사용 중인 파일은 복구 이동하지 않습니다. 잠시 후 다시 시도하세요."
                    )
                if repository.recovery_state(entry.asset) != "live":
                    raise AttachmentOperationError(
                        "보존 참조를 확인할 수 없어 격리 자료를 남겼습니다."
                    )
                if not entry.asset.removable:
                    continue
                original = self._path(entry.asset.path)
                quarantine = self._path(self._quarantine(command.job_id, entry.asset))
                if quarantine.exists():
                    if original.exists() or entry.state != "prepared":
                        raise AttachmentOperationError(
                            "복구할 원경로가 이미 사용 중입니다. 덮어쓰지 않았습니다."
                        )
                    with exclusive_read(
                        self._path(self._quarantine(command.job_id, entry.asset), required=True)
                    ) as stream:
                        if file_identity(os.fstat(stream.fileno())) != entry.identity:
                            raise AttachmentOperationError("복구 파일의 identity가 변경되었습니다.")
                        original.parent.mkdir(parents=True, exist_ok=True)
                        self._path(entry.asset.path)
                        rename_open_file(stream, original)
                elif entry.state == "prepared":
                    if (
                        not original.is_file()
                        or file_identity(self._path(entry.asset.path, required=True).stat())
                        != entry.identity
                    ):
                        raise AttachmentOperationError("복구할 파일이 누락되었거나 변경되었습니다.")
                elif entry.state == "missing" and original.exists():
                    raise AttachmentOperationError("누락 경로에 새 파일이 있어 확인이 필요합니다.")
            self.checkpoint("restored")
        self._finish_journal(command, "rolled_back")

    def finish(
        self,
        commit: DeleteCommit,
        repository: DeleteRepository,
        *,
        canceled: Callable[[], bool] = lambda: False,
    ) -> None:
        command = commit.command
        folder = self._path(self._folder(command.job_id))
        if not folder.exists():
            if any(
                asset.removable and repository.recovery_state(asset) != "absent"
                for asset in commit.assets
            ):
                self.coordinator.protect(
                    self._owner(command.job_id), tuple(asset.path for asset in commit.assets)
                )
                raise AttachmentOperationError(
                    "삭제 완료 저널이 없어 남은 자산을 정리하지 않았습니다."
                )
            self.coordinator.unprotect(self._owner(command.job_id))
            return
        actual, entries = self._read(command.job_id)
        if (
            actual != command
            or tuple(entry.asset for entry in entries) != commit.assets
            or repository.committed(command) != commit
        ):
            raise AttachmentOperationError("삭제 완료 기록과 격리 자료가 일치하지 않습니다.")
        completed = self._finished_mode(command)
        if completed is not None:
            if completed != "deleted" or any(
                entry.asset.removable
                and (
                    repository.recovery_state(entry.asset) != "absent"
                    or self._path(self._quarantine(command.job_id, entry.asset)).exists()
                )
                for entry in entries
            ):
                raise AttachmentOperationError("물리 삭제 완료 기록을 확인할 수 없습니다.")
            self._finish_journal(command, "deleted")
            return
        for entry in entries:
            if canceled():
                raise AttachmentOperationError(
                    "파일 정리를 중단했습니다. 확정 기록과 남은 자료를 보존합니다."
                )
            if not entry.asset.removable:
                continue
            with self.coordinator.gate:
                self.coordinator.assert_accessible(
                    entry.asset.path, owner=self._owner(command.job_id)
                )
                if self.coordinator.is_pinned(entry.asset.path) or self.coordinator.is_pinned(
                    self._quarantine(command.job_id, entry.asset)
                ):
                    raise AttachmentOperationError(
                        "사용 중인 파일은 물리 정리하지 않습니다. 잠시 후 다시 시도하세요."
                    )
                state = repository.recovery_state(entry.asset)
                if state == "live":
                    raise AttachmentOperationError("보존 참조가 남아 파일을 삭제하지 않았습니다.")
                original = self._path(entry.asset.path)
                quarantine = self._path(self._quarantine(command.job_id, entry.asset))
                if original.exists():
                    raise AttachmentOperationError("삭제 원경로에 다른 자료가 생겨 보존했습니다.")
                if quarantine.exists():
                    if entry.state != "prepared" or state != "pending":
                        raise AttachmentOperationError("정리할 격리 자료의 상태가 불명확합니다.")
                    with exclusive_read(
                        self._path(self._quarantine(command.job_id, entry.asset), required=True)
                    ) as stream:
                        if file_identity(os.fstat(stream.fileno())) != entry.identity:
                            raise AttachmentOperationError("격리 후 파일이 변경되어 보존했습니다.")
                        delete_open_file(stream)
                elif entry.state not in {"prepared", "missing"}:
                    raise AttachmentOperationError("삭제 파일의 준비 상태를 확인할 수 없습니다.")
            self.checkpoint("purged")
            with self.coordinator.gate:
                repository.finish_asset(commit, entry.asset)
            self.checkpoint("asset_removed")
        self._finish_journal(command, "deleted")

    def protect_pending(self, job_id: str) -> None:
        try:
            folder = self._path(self._folder(job_id))
            if not folder.exists():
                return  # No journal/bytes were created. Do not unblock other jobs.
            _, entries = self._read(job_id, with_states=False)
            paths = tuple(
                path
                for entry in entries
                for path in (entry.asset.path, self._quarantine(job_id, entry.asset))
            )
            self.coordinator.protect(self._owner(job_id), paths)
        except Exception:
            self.coordinator.protect(self._owner(job_id), None)

    def has_pending(self) -> bool:
        root = self._path(".trash")
        if not root.exists():
            return False
        with os.scandir(root) as folders:
            return any(folder.name.startswith("delete-") for folder in folders)

    def preflight(
        self, *, limit: int = 100, budget_seconds: float = 2.0
    ) -> tuple[DeleteRecoveryItem, ...]:
        """Bounded journal-only reservation; no DB queries, hashes, or file moves."""
        start = monotonic()
        items: list[DeleteRecoveryItem] = []
        try:
            root = self._path(".trash")
            if not root.exists():
                return ()
            with os.scandir(root) as folders:
                for folder in folders:
                    if not folder.name.startswith("delete-"):
                        continue
                    if len(items) >= limit or monotonic() - start > budget_seconds:
                        self.coordinator.protect("delete:overflow", None)
                        items.append(
                            DeleteRecoveryItem(
                                "overflow",
                                None,
                                "중단된 삭제가 많아 추가 안전 확인이 필요합니다. 파일 영역을 보호했습니다.",
                            )
                        )
                        break
                    job_id = folder.name.removeprefix("delete-")
                    try:
                        command, _ = self._read(job_id, with_states=False)
                        self.protect_pending(job_id)
                        items.append(
                            DeleteRecoveryItem(
                                job_id,
                                command,
                                "중단된 삭제 자료를 보호하고 복구 상태를 확인합니다.",
                            )
                        )
                    except Exception:
                        self.coordinator.protect(self._owner(job_id), None)
                        items.append(
                            DeleteRecoveryItem(
                                job_id,
                                None,
                                "삭제 저널을 확인할 수 없어 파일 영역을 보호했습니다: "
                                + folder.name,
                            )
                        )
        except Exception:
            self.coordinator.protect("delete:unreadable", None)
            items.append(
                DeleteRecoveryItem(
                    "unreadable", None, "삭제 작업 영역을 확인하지 못해 파일 영역을 보호했습니다."
                )
            )
        return tuple(items)
