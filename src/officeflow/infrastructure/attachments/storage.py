from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from officeflow.application.attachments import (
    AttachmentCanceledError,
    AttachmentOperationError,
    QuarantinedAttachment,
    StoredAttachment,
)

_SAFE_EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,16}$")
_COPY_CHUNK_SIZE = 1024 * 1024


class ManagedAttachmentStorage:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._quarantine_root = self._root / ".trash"

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

    def checksum(self, relative_path: str) -> str:
        path = self.resolve(relative_path)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(_COPY_CHUNK_SIZE):
                digest.update(chunk)
        return digest.hexdigest()

    def remove(self, relative_path: str) -> None:
        path = self.resolve(relative_path, require_exists=False)
        path.unlink(missing_ok=True)
        self._remove_empty_parents(path.parent)

    def quarantine(self, relative_path: str) -> QuarantinedAttachment:
        source = self.resolve(relative_path)
        quarantine_relative = f".trash/{uuid4().hex}/{source.name}"
        destination = self.resolve(quarantine_relative, require_exists=False)
        destination.parent.mkdir(parents=True, exist_ok=False)
        os.replace(source, destination)
        self._remove_empty_parents(source.parent)
        return QuarantinedAttachment(relative_path, quarantine_relative)

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
        self._remove_empty_parents(source.parent)

    def purge(self, quarantined: QuarantinedAttachment) -> None:
        path = self.resolve(quarantined.quarantine_relative_path, require_exists=False)
        path.unlink(missing_ok=True)
        self._remove_empty_parents(path.parent)

    def _remove_empty_parents(self, directory: Path) -> None:
        current = directory
        while current != self._root and current != self._quarantine_root:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent
