"""Owned internal journals and protected streaming captures, independent of exports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import BinaryIO, cast
from uuid import uuid4

from officeflow.application.attachment_documents import (
    CommittedDocument,
    DocumentCommand,
    DocumentOperation,
    DocumentSource,
    InternalCapture,
    InternalRecoveryItem,
)
from officeflow.application.attachments import AttachmentCanceledError, AttachmentOperationError
from officeflow.application.file_operations import FileOperationCoordinator
from officeflow.infrastructure.attachments.read_lease import (
    delete_open_file,
    exclusive_read,
    file_identity,
    protected_read,
    rename_open_file,
)

_CHUNK = 1024 * 1024
_ID = re.compile(r"[0-9a-f]{32}")
_HASH = re.compile(r"[0-9a-f]{64}")
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,16}")
_MAX_JOURNAL = 32 * 1024


def _decode_command(raw: object) -> DocumentCommand:
    if not isinstance(raw, dict) or set(raw) != {
        "job_id",
        "operation",
        "source_kind",
        "source_id",
        "expected_revision",
        "filename",
        "label",
        "memo",
        "cache_payload",
    }:
        raise ValueError("내부 명령 형식이 올바르지 않습니다.")
    if any(
        type(raw[key]) is not str
        for key in (
            "job_id",
            "operation",
            "source_kind",
            "filename",
            "label",
            "memo",
            "cache_payload",
        )
    ) or any(type(raw[key]) is not int for key in ("source_id", "expected_revision")):
        raise ValueError("내부 명령 값이 올바르지 않습니다.")
    return DocumentCommand(
        raw["job_id"],
        DocumentOperation(raw["operation"]),
        raw["source_kind"],
        raw["source_id"],
        raw["expected_revision"],
        raw["filename"],
        raw["label"],
        raw["memo"],
        raw["cache_payload"],
    )


def checked_managed_path(root: Path, relative: str, *, file_required: bool = False) -> Path:
    """Reject *all* reparse components, even links that currently remain in root."""
    pure = PurePosixPath(relative)
    if (
        not relative
        or pure.is_absolute()
        or "\\" in relative
        or ":" in relative
        or ".." in pure.parts
        or pure.as_posix() != relative
        or any(part.endswith((" ", ".")) for part in pure.parts)
    ):
        raise AttachmentOperationError("안전하지 않은 내부 작업 경로입니다.")
    current = root
    for part in pure.parts:
        current /= part
        try:
            value = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
            raise AttachmentOperationError("연결/junction 경로의 내부 작업은 허용하지 않습니다.")
        if current != root / pure and not stat.S_ISDIR(value.st_mode):
            raise AttachmentOperationError("내부 경로의 상위 항목이 폴더가 아닙니다.")
    if file_required:
        value = current.stat()
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise AttachmentOperationError("독립적인 일반 파일만 내부 작업에 사용할 수 있습니다.")
    return current


class InternalFileStorage:
    def __init__(
        self,
        root: Path,
        *,
        coordinator: FileOperationCoordinator,
        checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.coordinator = coordinator
        self._checkpoint = checkpoint
        self._open_targets: dict[str, BinaryIO] = {}

    @staticmethod
    def owner(job_id: str) -> str:
        return "internal:" + job_id

    def _check(self, stage: str) -> None:
        if self._checkpoint:
            self._checkpoint(stage)

    def _path(self, relative: str, *, file_required: bool = False) -> Path:
        return checked_managed_path(self.root, relative, file_required=file_required)

    def _journal_path(self, job_id: str) -> Path:
        if not _ID.fullmatch(job_id):
            raise ValueError("잘못된 내부 작업 ID입니다.")
        return self._path(f".operations/{job_id}/journal.json")

    def _write(self, payload: dict[str, object]) -> None:
        journal = self._journal_path(str(payload["job_id"]))
        temporary = journal.with_name(f"journal-{uuid4().hex}.tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, journal)

    def _read(self, job_id: str) -> dict[str, object]:
        journal = self._journal_path(job_id)
        self._path(journal.relative_to(self.root).as_posix(), file_required=True)
        if journal.stat().st_size > _MAX_JOURNAL:
            raise ValueError("내부 작업 기록이 너무 큽니다.")
        payload = json.loads(journal.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or payload.get("job_id") != job_id
        ):
            raise ValueError("지원하지 않는 내부 작업 기록입니다.")
        raw_command = payload.get("command")
        if not isinstance(raw_command, dict):
            raise ValueError("내부 작업의 명령 정보가 없습니다.")
        command = _decode_command(raw_command)
        if (
            command.job_id != job_id
            or payload.get("request_fingerprint") != command.request_fingerprint
        ):
            raise ValueError("내부 작업 선택 정보가 일치하지 않습니다.")
        asset_id = str(payload.get("asset_id", ""))
        if (
            not _ID.fullmatch(asset_id)
            or not _ID.fullmatch(str(payload.get("source_asset_id", "")))
            or not _HASH.fullmatch(str(payload.get("source_fingerprint", "")))
        ):
            raise ValueError("내부 자산 ID가 올바르지 않습니다.")
        suffix = Path(str(payload.get("output", ""))).suffix
        if suffix and not _EXTENSION.fullmatch(suffix):
            raise ValueError("내부 확장자가 올바르지 않습니다.")
        prefix = "frozen/" if command.operation.creates_frozen else ""
        expected_output = f"{prefix}{asset_id[:2]}/{asset_id[2:4]}/{asset_id}{suffix}"
        if (
            payload.get("output") != expected_output
            or payload.get("temporary") != f".operations/{job_id}/capture.part"
        ):
            raise ValueError("내부 작업의 소유 경로가 일치하지 않습니다.")
        for name in ("output", "temporary", "source"):
            self._path(str(payload[name]))
        source = PurePosixPath(str(payload["source"]))
        if any(part.startswith(".") for part in source.parts):
            raise ValueError("임시/격리 파일은 일반 문서 명령의 출처가 아닙니다.")
        if payload.get("state") not in {"copying", "prepared", "published"}:
            raise ValueError("알 수 없는 내부 작업 단계입니다.")
        if payload["state"] != "copying":
            identity = payload.get("identity")
            if (
                not isinstance(identity, list)
                or len(identity) != 4
                or any(type(item) is not int or item < 0 for item in identity)
                or type(payload.get("size")) is not int
                or cast(int, payload["size"]) < 0
                or not _HASH.fullmatch(str(payload.get("checksum", "")))
            ):
                raise ValueError("내부 작업의 검증값이 올바르지 않습니다.")
        return cast(dict[str, object], payload)

    @contextmanager
    def capture(
        self, command: DocumentCommand, source: DocumentSource, cancel_requested: Callable[[], bool]
    ) -> Iterator[InternalCapture]:
        journal = self._journal_path(command.job_id)
        # Never replace a former command's staging area, even if its receipt is absent.
        journal.parent.parent.mkdir(parents=True, exist_ok=True)
        journal.parent.mkdir(exist_ok=False)
        asset_id = uuid4().hex
        name = command.filename if command.operation is DocumentOperation.WORK else source.name
        suffix = Path(name).suffix.lower()
        suffix = suffix if _EXTENSION.fullmatch(suffix) else ""
        prefix = "frozen/" if command.operation.creates_frozen else ""
        relative = f"{prefix}{asset_id[:2]}/{asset_id[2:4]}/{asset_id}{suffix}"
        temporary_relative = f".operations/{command.job_id}/capture.part"
        payload: dict[str, object] = {
            "version": 1,
            "job_id": command.job_id,
            "command": asdict(command),
            "request_fingerprint": command.request_fingerprint,
            "source": source.relative_path,
            "source_asset_id": source.asset_id,
            "source_fingerprint": source.state_fingerprint,
            "asset_id": asset_id,
            "output": relative,
            "temporary": temporary_relative,
            "state": "copying",
        }
        self._write(payload)
        self._check("journal")
        source_path = self._path(source.relative_path, file_required=True)
        temporary = self._path(temporary_relative)
        digest = hashlib.sha256()
        size = 0
        with protected_read(source_path) as origin:
            before = file_identity(os.fstat(origin.fileno()))
            with temporary.open("xb") as target:
                while chunk := origin.read(_CHUNK):
                    if cancel_requested():
                        raise AttachmentCanceledError("내부 파일 복사를 취소했습니다.")
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    self._check("copy_chunk")
                target.flush()
                os.fsync(target.fileno())
            if before != file_identity(os.fstat(origin.fileno())) or before != file_identity(
                source_path.stat()
            ):
                raise AttachmentOperationError("복사 중 출처 파일이 변경되었습니다.")
            checksum = digest.hexdigest()
            if source.fixed_checksum is not None and (
                source.fixed_checksum != checksum or source.fixed_size != size
            ):
                raise AttachmentOperationError(
                    "보관 후 내용이 변경되었습니다. 기준 파일을 보존했습니다."
                )
            with exclusive_read(self._path(temporary_relative, file_required=True)) as target:
                identity = file_identity(os.fstat(target.fileno()))
                verified, actual_size = self._digest(target, cancel_requested)
                if (
                    verified != checksum
                    or actual_size != size
                    or identity != file_identity(temporary.stat())
                ):
                    raise AttachmentOperationError(
                        "준비 파일 검증에 실패했습니다. 자료를 보존했습니다."
                    )
                captured_at = datetime.now(UTC)
                payload.update(
                    state="prepared", identity=list(identity), size=size, checksum=checksum
                )
                self._write(payload)
                self._check("prepared")
                capture = InternalCapture(
                    command.job_id,
                    asset_id,
                    relative,
                    Path(relative).name,
                    size,
                    checksum,
                    captured_at,
                    identity,
                )
                self._open_targets[command.job_id] = target
                try:
                    yield capture
                finally:
                    self._open_targets.pop(command.job_id, None)

    @staticmethod
    def _digest(stream: BinaryIO, canceled: Callable[[], bool]) -> tuple[str, int]:
        stream.seek(0)
        digest, size = hashlib.sha256(), 0
        while chunk := stream.read(_CHUNK):
            if canceled():
                raise AttachmentCanceledError("내부 파일 검증을 취소했습니다.")
            digest.update(chunk)
            size += len(chunk)
        return digest.hexdigest(), size

    def publish(self, capture: InternalCapture) -> None:
        payload = self._read(capture.job_id)
        if payload["state"] != "prepared" or payload["asset_id"] != capture.asset_id:
            raise ValueError("공개할 준비 파일 기록이 일치하지 않습니다.")
        target = self._open_targets[capture.job_id]
        if file_identity(os.fstat(target.fileno())) != capture.identity:
            raise AttachmentOperationError("공개 전 준비 파일이 변경되었습니다.")
        destination = self._path(capture.relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._path(capture.relative_path)
        # FileRenameInfo has ReplaceIfExists=False. The verified handle survives
        # this rename and remains exclusive through DB commit.
        rename_open_file(target, destination)
        self._check("published_before_journal")
        payload["state"] = "published"
        self._write(payload)
        self._check("published")

    @contextmanager
    def resume_capture(
        self, command: DocumentCommand, source: DocumentSource, canceled: Callable[[], bool]
    ) -> Iterator[InternalCapture]:
        """User-authorized retry only. A prepared/published capture is never guessed."""
        payload = self._read(command.job_id)
        if (
            _decode_command(payload["command"]) != command
            or payload["source_fingerprint"] != source.state_fingerprint
            or payload["state"] not in {"prepared", "published"}
        ):
            raise ValueError("출처/준비 상태가 달라 재시도하지 않았습니다. 자료를 보존했습니다.")
        output, temporary = str(payload["output"]), str(payload["temporary"])
        published = self._path(output).exists()
        if published and self._path(temporary).exists():
            raise ValueError("준비/공개 파일이 모두 있어 소유 점검이 필요합니다.")
        actual = output if published else temporary
        with (
            protected_read(self._path(source.relative_path, file_required=True)) as origin,
            exclusive_read(self._path(actual, file_required=True)) as target,
        ):
            checksum, size = self._digest(target, canceled)
            if (
                file_identity(os.fstat(target.fileno()))
                != tuple(cast(list[int], payload["identity"]))
                or (size, checksum) != (payload["size"], payload["checksum"])
                or self._digest(origin, canceled) != (checksum, size)
            ):
                raise ValueError("출처/준비 내용이 달라 재시도하지 않았습니다.")
            capture = InternalCapture(
                command.job_id,
                str(payload["asset_id"]),
                output,
                Path(output).name,
                size,
                checksum,
                datetime.now(UTC),
                file_identity(os.fstat(target.fileno())),
            )
            if published:
                payload["state"] = "published"
                self._write(payload)
            self._open_targets[command.job_id] = target
            try:
                yield capture
            finally:
                self._open_targets.pop(command.job_id, None)

    def finish(self, capture: InternalCapture) -> None:
        self._check("committed")
        payload = self._read(capture.job_id)
        if payload["asset_id"] != capture.asset_id or payload["state"] != "published":
            raise ValueError("완료할 작업 기록이 일치하지 않습니다.")
        if (
            file_identity(self._path(capture.relative_path, file_required=True).stat())
            != capture.identity
        ):
            raise AttachmentOperationError("등록한 파일을 다시 확인해야 합니다.")
        if self._path(str(payload["temporary"])).exists():
            raise AttachmentOperationError("추가 준비 파일이 있어 기록을 보존했습니다.")
        if {item.name for item in self._journal_path(capture.job_id).parent.iterdir()} != {
            "journal.json"
        }:
            raise AttachmentOperationError("작업 폴더의 미분류 자료를 보존했습니다.")
        self._journal_path(capture.job_id).unlink()
        try:
            self._journal_path(capture.job_id).parent.rmdir()
        except OSError:
            # Unknown auxiliary files are not recursively removed.
            self.coordinator.protect(self.owner(capture.job_id), None)
            raise AttachmentOperationError("작업 폴더에 미분류 자료가 남아 있습니다.") from None
        self.coordinator.unprotect(self.owner(capture.job_id))

    def abort_unpublished(
        self, command: DocumentCommand, path_registered: Callable[[str], bool]
    ) -> None:
        journal = self._journal_path(command.job_id)
        if not journal.parent.exists():
            return
        payload = self._read(command.job_id)
        output = str(payload["output"])
        temporary = self._path(str(payload["temporary"]))
        if (
            payload["request_fingerprint"] != command.request_fingerprint
            or path_registered(output)
            or self._path(output).exists()
        ):
            raise AttachmentOperationError("공개/등록 여부 확인이 필요한 자료를 보존했습니다.")
        expected_names = (
            {"journal.json", "capture.part"} if temporary.exists() else {"journal.json"}
        )
        if {item.name for item in journal.parent.iterdir()} != expected_names:
            raise AttachmentOperationError("작업 폴더의 추가 자료를 보존했습니다.")
        if temporary.exists():
            if payload["state"] != "prepared":
                raise AttachmentOperationError("중단된 준비 파일의 내용 확인이 필요합니다.")
            with exclusive_read(
                self._path(str(payload["temporary"]), file_required=True)
            ) as stream:
                identity = list(file_identity(os.fstat(stream.fileno())))
                checksum, size = self._digest(stream, lambda: False)
                if (
                    identity != payload["identity"]
                    or checksum != payload["checksum"]
                    or size != payload["size"]
                ):
                    raise AttachmentOperationError("준비 파일이 변경되어 보존했습니다.")
                delete_open_file(stream)
        journal.unlink()
        journal.parent.rmdir()
        self.coordinator.unprotect(self.owner(command.job_id))

    def protect_pending(self, job_id: str) -> None:
        if not self._journal_path(job_id).parent.exists():
            return
        try:
            payload = self._read(job_id)
            self.coordinator.protect(
                self.owner(job_id),
                tuple(str(payload[key]) for key in ("source", "output", "temporary")),
            )
        except Exception:
            self.coordinator.protect(self.owner(job_id), None)

    def preflight(
        self, *, limit: int = 100, budget_seconds: float = 2.0
    ) -> tuple[InternalRecoveryItem, ...]:
        """No hashes/whole-asset queries. Overflow/unknown scope blocks file I/O."""
        try:
            return self._preflight(limit=limit, budget_seconds=budget_seconds)
        except (OSError, ValueError, AttachmentOperationError) as error:
            self.coordinator.protect("internal:unreadable", None)
            return (
                InternalRecoveryItem("", f"내부 작업 영역 확인 불가 · 파일 자료 보호 중: {error}"),
            )

    def _preflight(self, *, limit: int, budget_seconds: float) -> tuple[InternalRecoveryItem, ...]:
        root = self._path(".operations")
        if not root.exists():
            return ()
        deadline = monotonic() + budget_seconds
        items: list[InternalRecoveryItem] = []
        with os.scandir(root) as entries:
            for entry in entries:
                if len(items) >= limit or monotonic() >= deadline:
                    self.coordinator.protect("internal:overflow", None)
                    items.append(
                        InternalRecoveryItem("", "추가 내부 작업 점검 필요 · 파일 작업은 보호 중")
                    )
                    break
                try:
                    if not _ID.fullmatch(entry.name):
                        raise ValueError("소유가 불명확한 내부 작업 폴더")
                    payload = self._read(entry.name)
                    command = _decode_command(payload["command"])
                    self.protect_pending(entry.name)
                    items.append(
                        InternalRecoveryItem(
                            entry.name,
                            "중단된 내부 파일 작업 확인 필요",
                            command,
                            str(payload["source"]),
                            str(payload["output"]),
                        )
                    )
                except Exception as error:
                    self.coordinator.protect(self.owner(entry.name), None)
                    items.append(
                        InternalRecoveryItem(entry.name, f"내부 작업 기록을 보존했습니다: {error}")
                    )
        return tuple(items)

    def has_pending(self) -> bool:
        root = self._path(".operations")
        if not root.exists():
            return False
        with os.scandir(root) as entries:
            return next(entries, None) is not None

    def verify_committed(
        self,
        command: DocumentCommand,
        committed: CommittedDocument,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> str:
        """A missing/edited result never authorizes repeating a committed command."""
        relative = committed.relative_path
        if relative is None:
            return "이미 확정된 작업입니다. 결과 항목이 이후 삭제되었거나 상태가 변경되었습니다."
        owner = self.owner(command.job_id)
        try:
            with self.coordinator.pin(relative, owner=owner):
                path = self._path(relative, file_required=True)
                with protected_read(path) as stream:
                    before = file_identity(os.fstat(stream.fileno()))
                    checksum, size = self._digest(stream, cancel_requested or (lambda: False))
                    if before != file_identity(
                        os.fstat(stream.fileno())
                    ) or before != file_identity(path.stat()):
                        raise AttachmentOperationError("확인 중 결과 파일이 변경되었습니다.")
                if checksum != committed.checksum or size != committed.size:
                    return (
                        "이미 확정된 고정 파일의 내용이 달라 확인이 필요합니다."
                        if committed.frozen
                        else "이미 생성한 작업본의 내용이 이후 수정되었습니다. 다시 생성하지 않았습니다."
                    )
            journal = self._journal_path(command.job_id)
            if journal.is_file():
                payload = self._read(command.job_id)
                if (
                    payload["asset_id"] != committed.result.asset_id
                    or payload["request_fingerprint"] != command.request_fingerprint
                ):
                    raise ValueError("완료 영수증과 작업 기록이 일치하지 않습니다.")
                if self._path(str(payload["temporary"])).exists():
                    raise ValueError("미분류 준비 자료를 보존했습니다.")
                # Exact journal only; unknown files in the directory are preserved.
                if {item.name for item in journal.parent.iterdir()} != {"journal.json"}:
                    raise ValueError("작업 폴더의 추가 자료를 보존했습니다.")
                journal.unlink()
                journal.parent.rmdir()
            self.coordinator.unprotect(owner)
            return ""
        except Exception as error:
            self.protect_pending(command.job_id)
            return f"이미 확정된 작업입니다. 파일/기록 확인 필요: {error}"
