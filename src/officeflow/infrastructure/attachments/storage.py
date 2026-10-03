from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from time import monotonic
from uuid import uuid4

from officeflow.application.attachments import (
    AttachmentCanceledError,
    AttachmentOperationError,
    QuarantinedAttachment,
    StoredAttachment,
)
from officeflow.infrastructure.attachments.coordination import attachment_gate

_SAFE_EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,16}$")
_COPY_CHUNK_SIZE = 1024 * 1024


class ManagedAttachmentStorage:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._quarantine_root = self._root / ".trash"

    def operation_lock(self) -> AbstractContextManager[bool]:
        return attachment_gate(self._root)

    @property
    def root(self) -> Path:
        return self._root

    def import_file(
        self,
        source: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> StoredAttachment:
        try:
            source_path = source.resolve(strict=True)
        except (FileNotFoundError, OSError) as error:
            raise AttachmentOperationError("선택한 파일을 찾을 수 없습니다.") from error
        if not source_path.is_file():
            raise AttachmentOperationError("일반 파일만 첨부할 수 있습니다.")
        if cancel_requested is not None and cancel_requested():
            raise AttachmentCanceledError("첨부파일 복사를 취소했습니다.")

        token = uuid4().hex
        suffix = source_path.suffix if _SAFE_EXTENSION.fullmatch(source_path.suffix) else ""
        stored_name = f"{token}{suffix.lower()}"
        relative_path = f"{token[:2]}/{token[2:4]}/{stored_name}"
        destination = self.resolve(relative_path, require_exists=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{stored_name}.{uuid4().hex}.part")
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with source_path.open("rb") as source_stream, temporary.open("xb") as target_stream:
                while chunk := source_stream.read(_COPY_CHUNK_SIZE):
                    if cancel_requested is not None and cancel_requested():
                        raise AttachmentCanceledError("첨부파일 복사를 취소했습니다.")
                    target_stream.write(chunk)
                    digest.update(chunk)
                    size_bytes += len(chunk)
                target_stream.flush()
                os.fsync(target_stream.fileno())
            with self.operation_lock():
                os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return StoredAttachment(
            stored_name=stored_name,
            relative_path=relative_path,
            size_bytes=size_bytes,
            checksum=digest.hexdigest(),
        )

    def resolve(self, relative_path: str, *, require_exists: bool = True) -> Path:
        if not relative_path or "\\" in relative_path:
            raise AttachmentOperationError("안전하지 않은 첨부파일 경로입니다.")
        candidate = (self._root / relative_path).resolve(strict=False)
        if not candidate.is_relative_to(self._root) or candidate == self._root:
            raise AttachmentOperationError("첨부파일 경로가 관리 폴더를 벗어납니다.")
        if require_exists and (not candidate.exists() or not candidate.is_file()):
            raise FileNotFoundError(candidate)
        return candidate

    def exists(self, relative_path: str) -> bool:
        try:
            self.resolve(relative_path)
        except FileNotFoundError:
            return False
        return True

    def checksum(
        self, relative_path: str, *, cancel_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> str:
        path = self.resolve(relative_path)
        digest = hashlib.sha256()
        def identity(stat: os.stat_result) -> tuple[int, int, int, int]:
            # Windows fstat/stat have different ctime semantics on Python 3.12.
            return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            completed = 0
            last_progress = 0.0
            while True:
                if cancel_requested is not None and cancel_requested():
                    raise AttachmentCanceledError("무결성 검사를 취소했습니다.")
                chunk = stream.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                completed += len(chunk)
                if progress is not None and (monotonic() - last_progress >= 0.1 or completed >= before.st_size):
                    progress(completed, before.st_size)
                    last_progress = monotonic()
            if identity(before) != identity(os.fstat(stream.fileno())) or identity(before) != identity(path.stat()):
                raise AttachmentOperationError("검사 중 파일이 변경되었습니다. 파일 저장을 마친 뒤 다시 검사하세요.")
        return digest.hexdigest()

    def remove(self, relative_path: str) -> None:
        with self.operation_lock():
            path = self.resolve(relative_path, require_exists=False)
            path.unlink(missing_ok=True)
            self._remove_empty_parents(path.parent)

    def quarantine(self, relative_path: str) -> QuarantinedAttachment:
        with self.operation_lock():
            source = self.resolve(relative_path)
            if source.is_relative_to(self._quarantine_root):
                raise AttachmentOperationError("격리 파일은 다시 삭제 대상으로 옮길 수 없습니다.")
            quarantine_relative = f".trash/{uuid4().hex}/{source.name}"
            destination = self.resolve(quarantine_relative, require_exists=False)
            destination.parent.mkdir(parents=True, exist_ok=False)
            record = QuarantinedAttachment(relative_path, quarantine_relative)
            journal = destination.parent / "deletion.json"
            with journal.open("x", encoding="utf-8") as stream:
                json.dump({"original": relative_path, "quarantined": quarantine_relative}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(source, destination)
            self._remove_empty_parents(source.parent)
            return record

    def restore(self, quarantined: QuarantinedAttachment) -> None:
        source = self.resolve(quarantined.quarantine_relative_path)
        destination = self.resolve(
            quarantined.original_relative_path,
            require_exists=False,
        )
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        (source.parent / "deletion.json").unlink(missing_ok=True)
        self._remove_empty_parents(source.parent)

    def purge(self, quarantined: QuarantinedAttachment) -> None:
        path = self.resolve(quarantined.quarantine_relative_path, require_exists=False)
        path.unlink(missing_ok=True)
        (path.parent / "deletion.json").unlink(missing_ok=True)
        self._remove_empty_parents(path.parent)

    def recover_quarantine(self, tracked: set[str]) -> tuple[str, ...]:
        """Undo uncommitted deletions; purge only journaled, committed deletions.

        Legacy/unknown files are preserved unless their unique stored name maps
        to a missing DB reference. Invalid journals never authorize deletion.
        """
        warnings: list[str] = []
        with self.operation_lock():
            if not self._quarantine_root.exists():
                return ()
            for folder in tuple(self._quarantine_root.iterdir()):
                try:
                    if not folder.is_dir() or folder.is_symlink():
                        warnings.append(f"확인이 필요한 격리 파일 보존: {folder.name}")
                        continue
                    journal = folder / "deletion.json"
                    if journal.is_file():
                        if journal.is_symlink() or journal.stat().st_size > 8192:
                            raise ValueError("invalid deletion journal")
                        raw = json.loads(journal.read_text(encoding="utf-8"))
                        original, quarantined = raw["original"], raw["quarantined"]
                        if not isinstance(original, str) or not isinstance(quarantined, str):
                            raise ValueError("invalid deletion paths")
                        source = self.resolve(quarantined, require_exists=False)
                        destination = self.resolve(original, require_exists=False)
                        if (
                            source.parent != folder
                            or source.name != destination.name
                            or source.name == "deletion.json"
                            or source.is_symlink()
                            or destination.is_relative_to(self._quarantine_root)
                        ):
                            raise ValueError("invalid deletion scope")
                        record = QuarantinedAttachment(original, quarantined)
                        if original in tracked:
                            if source.is_file():
                                self.restore(record)
                            elif destination.is_file():
                                journal.unlink()
                                self._remove_empty_parents(folder)
                            else:
                                warnings.append(f"삭제 복구 원본 누락: {original}")
                        else:
                            self.purge(record)
                    else:
                        for source in tuple(folder.iterdir()):
                            if not source.is_file() or source.is_symlink():
                                continue
                            matches = [path for path in tracked if Path(path).name == source.name]
                            if len(matches) == 1 and not self.exists(matches[0]):
                                self.restore(
                                    QuarantinedAttachment(
                                        matches[0], source.relative_to(self._root).as_posix()
                                    )
                                )
                            else:
                                warnings.append(f"확인이 필요한 격리 파일 보존: {source.name}")
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    AttachmentOperationError,
                ) as error:
                    warnings.append(f"삭제 작업 복구 보류 ({folder.name}): {error}")
        return tuple(warnings)

    def _remove_empty_parents(self, directory: Path) -> None:
        current = directory
        while current != self._root and current != self._quarantine_root:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent
