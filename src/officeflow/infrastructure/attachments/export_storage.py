"""Stream, verify, publish without clobbering, and journal external file copies."""

import ctypes
import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from officeflow.application.attachment_export import (
    ExportDestinationError,
    ExportRecoveryItem,
    OverwritePrompt,
    PreparedExport,
    ResolvedFileReference,
)
from officeflow.application.attachments import AttachmentCanceledError, AttachmentOperationError
from officeflow.domain.attachment import FileObservation
from officeflow.infrastructure.attachments.read_lease import file_identity, protected_read

_CHUNK = 1024 * 1024
logger = logging.getLogger(__name__)
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE)
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ExternalExportStorage:
    def __init__(
        self,
        data_root: Path,
        *,
        checkpoint: Callable[[str, PreparedExport | None], None] | None = None,
    ) -> None:
        self.data_root = data_root.resolve()
        self.journal_root = self.data_root / "external-file-operations"
        self._checkpoint = checkpoint

    @staticmethod
    def new_job_id() -> str:
        return uuid4().hex

    @staticmethod
    def safe_name(name: str) -> str:
        cleaned = _INVALID.sub("_", name).rstrip(" .") or "첨부파일"
        if _RESERVED.match(cleaned):
            cleaned = "_" + cleaned
        suffix = Path(cleaned).suffix[:17]
        return cleaned if len(cleaned) <= 180 else cleaned[: 180 - len(suffix)] + suffix

    def validate_destination(self, path: Path, source: Path) -> Path:
        path = path.expanduser().resolve(strict=False)
        suffix = source.suffix
        if suffix and path.suffix.casefold() != suffix.casefold():
            # This is a copy, not a file-format conversion.
            path = path.with_name(path.name + suffix)
        if (
            path.is_relative_to(self.data_root)
            or path == source
            or (path.exists() and os.path.samefile(path, source))
        ):
            raise AttachmentOperationError(
                "관리 데이터 폴더 또는 출처와 같은 파일에는 저장할 수 없습니다."
            )
        if (
            not path.parent.is_dir()
            or path.is_dir()
            or _INVALID.search(path.name)
            or _RESERVED.match(path.name)
            or path.name.endswith((" ", "."))
            or len(path.name) > 240
        ):
            raise AttachmentOperationError("저장 폴더 또는 Windows 파일 이름이 올바르지 않습니다.")
        return path

    def _write_journal(
        self,
        prepared: PreparedExport,
        state: str,
        *,
        backup: Path | None = None,
        original_checksum: str | None = None,
    ) -> None:
        self.journal_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "job_id": prepared.job_id,
            "temporary": str(prepared.temporary),
            "destination": str(prepared.destination),
            "sha256": prepared.observation.checksum,
            "size": prepared.observation.size_bytes,
            "state": state,
            "backup": str(backup) if backup else None,
            "original_sha256": original_checksum,
            "temporary_identity": prepared.temporary_identity,
        }
        temporary = prepared.journal.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, prepared.journal)
        if self._checkpoint:
            self._checkpoint(state, prepared)

    @staticmethod
    def _digest(
        path: Path, *, cancel_requested: Callable[[], bool] | None = None
    ) -> tuple[str, tuple[int, int, int, int]]:
        digest = hashlib.sha256()
        with protected_read(path) as stream:
            before = os.fstat(stream.fileno())
            while chunk := stream.read(_CHUNK):
                if cancel_requested and cancel_requested():
                    raise AttachmentCanceledError("파일 저장을 취소했습니다.")
                digest.update(chunk)
            if file_identity(before) != file_identity(os.fstat(stream.fileno())) or file_identity(
                before
            ) != file_identity(path.stat()):
                raise AttachmentOperationError(
                    "읽는 동안 파일이 변경되었습니다. 저장하고 문서를 닫은 뒤 다시 시도하세요."
                )
        return digest.hexdigest(), file_identity(before)

    def prepare(
        self,
        resolved: ResolvedFileReference,
        destination: Path,
        job_id: str,
        *,
        cancel_requested: Callable[[], bool],
        progress: Callable[[int, int], None] | None,
    ) -> PreparedExport:
        token = uuid4().hex
        temporary = destination.parent / f".officeflow-{job_id}-{token}.part"
        journal = self.journal_root / f"{job_id}-{token}.json"
        self.journal_root.mkdir(parents=True, exist_ok=True)
        # A copying checkpoint owns exact paths but has no complete-content hash.
        initial = PreparedExport(
            job_id,
            temporary,
            destination,
            journal,
            FileObservation(0, "0" * 64, 0, datetime.now(UTC)),
            (0, 0),
        )
        self._write_journal(initial, "copying")
        digest = hashlib.sha256()
        total = 0
        last_progress = 0.0
        created = False
        try:
            with protected_read(resolved.path) as source:
                before = os.fstat(source.fileno())
                try:
                    target_stream = temporary.open("xb")
                except OSError as error:
                    raise ExportDestinationError(
                        error.errno,
                        f"저장 폴더에 준비 파일을 만들지 못했습니다: {error}",
                        str(destination.parent),
                    ) from error
                with target_stream as target:
                    created = True
                    temp_stat = os.fstat(target.fileno())
                    identity = (temp_stat.st_dev, temp_stat.st_ino)
                    initial = PreparedExport(
                        job_id,
                        temporary,
                        destination,
                        journal,
                        initial.observation,
                        (0, 0),
                        identity,
                    )
                    self._write_journal(initial, "copying")
                    while chunk := source.read(_CHUNK):
                        if cancel_requested():
                            raise AttachmentCanceledError("파일 저장을 취소했습니다.")
                        target.write(chunk)
                        digest.update(chunk)
                        total += len(chunk)
                        if progress and (
                            monotonic() - last_progress >= 0.1 or total == before.st_size
                        ):
                            progress(total, before.st_size)
                            last_progress = monotonic()
                    target.flush()
                    os.fsync(target.fileno())
                if file_identity(before) != file_identity(
                    os.fstat(source.fileno())
                ) or file_identity(before) != file_identity(resolved.path.stat()):
                    raise AttachmentOperationError(
                        "복사 중 출처가 변경되었습니다. 저장을 마친 뒤 다시 시도하세요."
                    )
                observation = FileObservation(
                    total, digest.hexdigest(), before.st_mtime_ns, datetime.now(UTC)
                )
                prepared = PreparedExport(
                    job_id,
                    temporary,
                    destination,
                    journal,
                    observation,
                    (before.st_dev, before.st_ino),
                    identity,
                )
                copied_hash, copied_stat = self._digest(
                    temporary, cancel_requested=cancel_requested
                )
                if copied_hash != observation.checksum or copied_stat[2] != total:
                    raise AttachmentOperationError(
                        "준비 사본 검증에 실패했습니다. 출처는 그대로 유지됩니다."
                    )
            self._write_journal(prepared, "prepared")
            return prepared
        except BaseException:
            # Only this invocation's exclusively-created temporary is removed.
            # Never remove a final output or an existing user file here.
            try:
                if created:
                    temporary.unlink(missing_ok=True)
                journal.unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "취소된 외부 사본의 준비 파일 정리가 보류되었습니다. 작업 기록을 보존합니다."
                )
            raise

    @staticmethod
    def _publish_new(temporary: Path, destination: Path) -> None:
        if os.name == "nt":
            os.rename(temporary, destination)  # Windows: fails if destination exists.
        else:
            os.link(temporary, destination)
            temporary.unlink()

    @staticmethod
    def _replace(destination: Path, temporary: Path, backup: Path) -> None:
        if os.name != "nt":
            raise AttachmentOperationError(
                "안전한 덮어쓰기는 Windows 파일 교체가 필요합니다. 다른 이름으로 저장하세요."
            )
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
        api.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]
        api.restype = wintypes.BOOL
        if not api(str(destination), str(temporary), str(backup), 0, None, None):
            raise ctypes.WinError(ctypes.get_last_error())

    def publish(
        self,
        prepared: PreparedExport,
        *,
        numbered: bool,
        cancel_requested: Callable[[], bool],
        confirm_overwrite: Callable[[OverwritePrompt], bool] | None,
    ) -> tuple[Path, str]:
        if cancel_requested():
            raise AttachmentCanceledError("파일 저장을 취소했습니다.")
        ready_hash, ready_identity = self._digest(
            prepared.temporary, cancel_requested=cancel_requested
        )
        if (
            ready_hash != prepared.observation.checksum
            or ready_identity[2] != prepared.observation.size_bytes
            or (prepared.temporary_identity and ready_identity[:2] != prepared.temporary_identity)
        ):
            raise AttachmentOperationError(
                f"준비 후 파일이 바뀌었습니다. 자료를 보존했습니다: {prepared.temporary}"
            )
        base = prepared.destination
        # Never use exists()+replace() for a new output.
        for number in range(101 if numbered else 1):
            destination = (
                base if number == 0 else base.with_name(f"{base.stem} ({number}){base.suffix}")
            )
            current = PreparedExport(
                prepared.job_id,
                prepared.temporary,
                destination,
                prepared.journal,
                prepared.observation,
                prepared.source_identity,
                prepared.temporary_identity,
            )
            if numbered and any(
                path.name.casefold() == destination.name.casefold()
                for path in destination.parent.iterdir()
            ):
                continue
            if cancel_requested():
                raise AttachmentCanceledError("파일 저장을 취소했습니다.")
            self._write_journal(current, "publishing")
            try:
                self._publish_new(current.temporary, destination)
            except FileExistsError:
                if numbered:
                    continue
                # Before a replacement checkpoint no final output was changed.
                # Ordinary lock/consent failures may safely discard our part.
                try:
                    return self._overwrite(current, cancel_requested, confirm_overwrite)
                except OSError:
                    self.abort(current)
                    raise
            except OSError:
                # No final was created by rename failure. Release a known part.
                self.abort(current)
                raise
            return destination, self._finish(current)
        self.abort(prepared)
        raise AttachmentOperationError(
            "같은 이름의 저장 경쟁이 너무 많습니다. 다른 폴더를 선택하세요."
        )

    def _overwrite(
        self,
        prepared: PreparedExport,
        cancel_requested: Callable[[], bool],
        confirm: Callable[[OverwritePrompt], bool] | None,
    ) -> tuple[Path, str]:
        if confirm is None:
            self.abort(prepared)
            raise FileExistsError("기존 파일을 덮어쓰려면 명시적 확인이 필요합니다.")
        changed = False
        for _attempt in range(3):
            old_hash, old_identity = self._digest(
                prepared.destination, cancel_requested=cancel_requested
            )
            if old_identity[:2] == prepared.source_identity:
                self.abort(prepared)
                raise AttachmentOperationError("출처와 동일한 물리 파일은 덮어쓸 수 없습니다.")
            if (
                not confirm(
                    OverwritePrompt(prepared.destination, old_identity[2], old_hash, changed)
                )
                or cancel_requested()
            ):
                raise AttachmentCanceledError("덮어쓰기를 취소했습니다.")
            fresh_hash, fresh_identity = self._digest(
                prepared.destination, cancel_requested=cancel_requested
            )
            if (fresh_hash, fresh_identity) != (old_hash, old_identity):
                changed = True
                continue
            backup = prepared.destination.parent / (prepared.temporary.stem + ".previous")
            self._write_journal(prepared, "replacing", backup=backup, original_checksum=old_hash)
            try:
                self._replace(prepared.destination, prepared.temporary, backup)
                saved_hash, _saved_identity = self._digest(prepared.destination)
                previous_hash, _previous_identity = self._digest(backup)
                if saved_hash != prepared.observation.checksum or previous_hash != old_hash:
                    raise AttachmentOperationError(
                        "교체 시 다른 프로그램이 파일을 변경했습니다. 이전/새 파일을 보존했습니다."
                    )
            except Exception as error:
                # Some ReplaceFileW errors move the original to backup first.
                # Restore only into a missing name, never over a changed output.
                if not prepared.destination.exists() and backup.is_file():
                    try:
                        recovery_hash, _stat = self._digest(backup)
                        if recovery_hash == old_hash:
                            self._publish_new(backup, prepared.destination)
                    except OSError:
                        logger.warning(
                            "외부 파일 교체 복구가 보류되었습니다. 기존/준비 파일과 작업 기록을 보존합니다."
                        )
                raise AttachmentOperationError(
                    f"교체 결과 확인 필요: {error}\n기존/새 자료 위치: {prepared.destination}\n복구 파일: {backup}\n준비 파일: {prepared.temporary}"
                ) from error
            try:
                self._write_journal(
                    prepared, "published", backup=backup, original_checksum=old_hash
                )
            except OSError:
                return (
                    prepared.destination,
                    f"저장 완료 · 복구 파일/작업 기록 정리 확인 필요: {backup}\n{prepared.journal}",
                )
            try:
                # Do not discard a recovery file edited after verification.
                checked_hash, checked_identity = self._digest(backup)
                if checked_hash != old_hash or checked_identity != _previous_identity:
                    return prepared.destination, f"저장 완료 · 변경된 복구 파일 보존: {backup}"
                backup.unlink()
            except OSError:
                return prepared.destination, f"저장 완료 · 복구 파일 정리 확인 필요: {backup}"
            return prepared.destination, self._finish(prepared)
        self.abort(prepared)
        raise AttachmentOperationError(
            "대상이 계속 변경되고 있습니다. 문서를 닫거나 다른 이름으로 저장하세요."
        )

    def _finish(self, prepared: PreparedExport) -> str:
        digest, stat = self._digest(prepared.destination)
        if digest != prepared.observation.checksum or stat[2] != prepared.observation.size_bytes:
            raise AttachmentOperationError(
                f"출력 내용 확인 필요: {prepared.destination}. 파일은 삭제하지 않았습니다."
            )
        try:
            self._write_journal(prepared, "published")
            prepared.journal.unlink()
        except OSError:
            return f"저장 완료 · 작업 기록 정리 확인 필요: {prepared.journal}"
        return ""

    def abort(self, prepared: PreparedExport) -> None:
        if prepared.temporary.exists():
            digest, stat = self._digest(prepared.temporary)
            if (
                digest != prepared.observation.checksum
                or stat[2] != prepared.observation.size_bytes
                or (
                    prepared.temporary_identity is not None
                    and stat[:2] != prepared.temporary_identity
                )
            ):
                raise AttachmentOperationError(
                    f"변경된 준비 파일을 보존했습니다: {prepared.temporary}"
                )
            prepared.temporary.unlink()
        prepared.journal.unlink(missing_ok=True)

    def _read_journal(
        self, journal: Path
    ) -> tuple[dict[str, object], str, Path, Path, Path | None]:
        if (
            journal.parent != self.journal_root
            or journal.is_symlink()
            or journal.stat().st_size > 16384
        ):
            raise ValueError("invalid journal")
        content = journal.read_bytes()
        raw = json.loads(content)
        job = raw["job_id"]
        if (
            not isinstance(job, str)
            or not re.fullmatch("[0-9a-f]{32}", job)
            or not re.fullmatch(job + r"-[0-9a-f]{32}\.json", journal.name)
        ):
            raise ValueError("invalid owner")
        temporary, destination = Path(raw["temporary"]), Path(raw["destination"])
        backup = Path(raw["backup"]) if raw.get("backup") else None
        token = journal.stem[len(job) + 1 :]
        if (
            raw.get("version") != 1
            or not temporary.is_absolute()
            or not destination.is_absolute()
            or temporary.is_symlink()
            or destination.is_symlink()
            or (backup and backup.is_symlink())
            or temporary.parent != destination.parent
            or temporary.name != f".officeflow-{job}-{token}.part"
            or destination.resolve().is_relative_to(self.data_root)
            or (backup and backup != temporary.with_suffix(".previous"))
        ):
            raise ValueError("invalid paths")
        if (
            raw.get("state") not in {"copying", "prepared", "publishing", "replacing", "published"}
            or not isinstance(raw.get("size"), int)
            or raw["size"] < 0
            or not isinstance(raw.get("sha256"), str)
            or not re.fullmatch("[0-9a-f]{64}", raw["sha256"])
        ):
            raise ValueError("invalid state")
        return raw, hashlib.sha256(content).hexdigest(), temporary, destination, backup

    def has_pending_operations(self) -> bool:
        return (
            self.journal_root.is_dir() and next(self.journal_root.glob("*.json"), None) is not None
        )

    def inspect_recovery(
        self, cancel_requested: Callable[[], bool]
    ) -> tuple[ExportRecoveryItem, ...]:
        """Read only; exact journaled paths, never scan the user's folder."""
        if not self.journal_root.is_dir():
            return ()
        notices: list[ExportRecoveryItem] = []
        for journal in sorted(self.journal_root.glob("*.json"))[:200]:
            if cancel_requested():
                break
            try:
                raw, signature, temporary, destination, backup = self._read_journal(journal)
                snapshots = tuple(
                    (path, *self._digest(path, cancel_requested=cancel_requested))
                    for path in (temporary, destination, backup)
                    if path is not None and path.is_file()
                )
                contents = {path: (digest, stat) for path, digest, stat in snapshots}

                def matches(
                    path: Path,
                    checksum: object,
                    size: object = None,
                    lookup: dict[Path, tuple[str, tuple[int, int, int, int]]] = contents,
                ) -> bool:
                    found = lookup.get(path)
                    return (
                        found is not None
                        and found[0] == checksum
                        and (size is None or found[1][2] == size)
                    )

                saved = raw["state"] != "copying" and matches(
                    destination, raw["sha256"], raw["size"]
                )
                owned_part = not temporary.exists() or (
                    raw["state"] != "copying"
                    and matches(temporary, raw["sha256"], raw["size"])
                    and list(contents[temporary][1][:2]) == raw.get("temporary_identity")
                )
                owned_backup = (
                    backup is None
                    or not backup.exists()
                    or matches(backup, raw.get("original_sha256"))
                )
                restore = (
                    not destination.exists()
                    and backup is not None
                    and backup in contents
                    and owned_backup
                )
                restored = (
                    backup is not None
                    and not backup.exists()
                    and matches(destination, raw.get("original_sha256"))
                )
                cleanup = (
                    owned_part
                    and owned_backup
                    and (saved or restored or (backup is None and temporary in contents))
                )
                message = f"저장 중단 확인 필요 · {destination}\n준비 파일: {temporary}"
                if saved:
                    message = f"이미 저장됨 · 다시 저장하지 않아도 됩니다: {destination}"
                if backup:
                    message += f"\n교체 복구 파일: {backup}"
                if not cleanup and not restore:
                    message += (
                        "\n내용/소유를 확정하지 못한 파일은 보존합니다. 탐색기에서 직접 확인하세요."
                    )
                notices.append(
                    ExportRecoveryItem(
                        journal,
                        signature,
                        message + f"\n작업 기록: {journal}",
                        destination,
                        temporary,
                        backup,
                        snapshots,
                        cleanup,
                        restore,
                    )
                )
            except (OSError, ValueError, KeyError, TypeError):
                notices.append(
                    ExportRecoveryItem(
                        journal, "", f"분류하지 못한 저장 작업 기록을 보존했습니다: {journal}"
                    )
                )
        return tuple(notices)

    def pending_operations(self) -> tuple[str, ...]:
        return tuple(item.message for item in self.inspect_recovery(lambda: False))

    def recover(self, item: ExportRecoveryItem, *, restore: bool) -> str:
        if not (item.can_restore if restore else item.can_cleanup):
            raise AttachmentOperationError(
                "자동 처리할 수 없는 자료입니다. 위치를 열어 직접 확인하세요."
            )
        raw, signature, temporary, destination, backup = self._read_journal(item.journal)
        if signature != item.signature:
            raise AttachmentOperationError("작업 기록이 바뀌었습니다. 다시 확인하세요.")
        for path, checksum, identity in item.snapshots:
            if self._digest(path) != (checksum, identity):
                raise AttachmentOperationError(
                    "확인 후 파일이 바뀌었습니다. 자료를 보존했습니다. 다시 확인하세요."
                )
        expected_paths = {path for path, _, _ in item.snapshots}
        if any(
            path and path.exists() and path not in expected_paths
            for path in (temporary, destination, backup)
        ):
            raise AttachmentOperationError("새 파일이 생겼습니다. 덮어쓰지 않고 보존했습니다.")
        if restore:
            assert backup is not None
            self._publish_new(backup, destination)
            return f"기존 파일을 복구했습니다: {destination}\n준비 파일과 작업 기록은 보존했습니다."
        # Never delete/replace the final output. Only verified owned leftovers.
        if temporary in expected_paths:
            if raw["state"] == "copying" or list(
                dict((path, identity) for path, _, identity in item.snapshots)[temporary][:2]
            ) != raw.get("temporary_identity"):
                raise AttachmentOperationError("준비 파일 소유를 확인할 수 없습니다.")
            temporary.unlink()
        if backup is not None and backup in expected_paths:
            backup.unlink()
        item.journal.unlink()
        return (
            "확인된 준비/복구 파일과 작업 기록만 정리했습니다. 저장 결과 파일은 그대로 유지됩니다."
        )
