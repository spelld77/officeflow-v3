"""Verified, disposable drag snapshots; never expose managed attachment paths."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from officeflow.application.attachment_export import AttachmentExportService, FileReference
from officeflow.application.attachments import AttachmentCanceledError, AttachmentOperationError
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
from officeflow.infrastructure.attachments.read_lease import (
    exclusive_read,
    file_identity,
    protected_read,
)


@dataclass
class DragCopyBatch:
    directory: Path
    paths: tuple[Path, ...]
    size: int
    offered: bool = False


class DragCopyStore:
    MAX_FILES = 100
    MAX_BYTES = 1024**3
    MAX_BATCHES = 32

    def __init__(self, exports: AttachmentExportService, root: Path | None = None) -> None:
        self.exports = exports
        self.root = root or Path(tempfile.gettempdir()) / "OfficeFlow-drag-copies"
        self.session = self.root / uuid4().hex
        self._lease: AbstractContextManager[BinaryIO] | None = None
        self._batches: list[DragCopyBatch] = []

    @staticmethod
    def _linked(path: Path) -> bool:
        return path.is_symlink() or path.is_junction()

    def _initialize(self) -> None:
        if self._lease is not None:
            return
        if self._linked(self.root):
            raise AttachmentOperationError("드래그 임시 폴더를 확인하세요.")
        self.root.mkdir(parents=True, exist_ok=True)
        # Only old, unlocked, explicitly owned sessions can be reclaimed.
        for directory in islice(self.root.iterdir(), 64):
            if not directory.is_dir() or self._linked(directory):
                continue
            marker = directory / "lease.json"
            try:
                offered = directory / "last-offer"
                if self._linked(offered):
                    continue
                modified = max(
                    marker.stat().st_mtime, offered.stat().st_mtime if offered.exists() else 0
                )
                if self._linked(marker) or time.time() - modified < 86400:
                    continue
                with exclusive_read(marker) as stream:
                    if json.load(stream) != {"owner": "OfficeFlow-drag-copies-v1"}:
                        continue
                self._remove(directory)
            except (OSError, ValueError):
                continue
        self.session.mkdir()
        marker = self.session / "lease.json"
        marker.write_text('{"owner":"OfficeFlow-drag-copies-v1"}', encoding="utf-8")
        self._lease = protected_read(marker)
        self._lease.__enter__()

    def _remove(self, directory: Path) -> None:
        root = self.root.resolve()
        if (
            self._linked(directory)
            or directory.resolve() == root
            or not directory.resolve().is_relative_to(root)
        ):
            raise AttachmentOperationError("임시 사본 경로를 확인하세요.")
        if any(self._linked(path) for path in directory.rglob("*")):
            raise AttachmentOperationError("연결된 임시 사본은 자동으로 정리하지 않습니다.")
        shutil.rmtree(directory)

    def discard(self, batch: DragCopyBatch) -> None:
        if not batch.offered:
            self._remove(batch.directory)
            if batch in self._batches:
                self._batches.remove(batch)

    def offer(self, batch: DragCopyBatch) -> None:
        if batch not in self._batches:
            raise AttachmentOperationError("현재 준비된 사본이 아닙니다. 다시 선택하세요.")
        (self.session / "last-offer").touch()
        batch.offered = True

    def prepare(
        self, references: tuple[FileReference, ...], canceled: Callable[[], bool]
    ) -> DragCopyBatch:
        if (
            not references
            or len(references) > self.MAX_FILES
            or len(set(references)) != len(references)
        ):
            raise AttachmentOperationError(
                "한 번에 1~100개를 선택하세요. 더 많은 파일은 저장 기능을 사용하세요."
            )
        self._initialize()
        for old in tuple(self._batches):
            self.discard(old)
        if len(self._batches) >= self.MAX_BATCHES:
            raise AttachmentOperationError(
                "이 창의 드래그 사본 한도입니다. 저장 기능을 사용하세요."
            )
        used = sum(batch.size for batch in self._batches)
        directory = self.session / uuid4().hex
        directory.mkdir()
        paths: list[Path] = []
        names: set[str] = set()
        total = 0
        attachments = self.exports.attachments
        try:
            with attachments.coordinator.job(canceled, background=True):
                for reference in references:
                    if canceled():
                        raise AttachmentCanceledError("드래그 복사 준비를 취소했습니다.")
                    with attachments.coordinator.gate:
                        resolved = self.exports.resolve(reference)
                        pin = attachments.coordinator.pin(resolved.relative_path)
                        pin.__enter__()
                    try:
                        name = ExternalExportStorage.safe_name(resolved.name)
                        stem, suffix = Path(name).stem, Path(name).suffix
                        number = 2
                        while name.casefold() in names:
                            name = f"{stem} ({number}){suffix}"
                            number += 1
                        names.add(name.casefold())
                        target = directory / name
                        digest = hashlib.sha256()
                        size = 0
                        with protected_read(resolved.path) as source, target.open("xb") as output:
                            before = os.fstat(source.fileno())
                            if used + total + before.st_size > self.MAX_BYTES:
                                raise AttachmentOperationError(
                                    "드래그 사본 용량 한도입니다. 다른 이름으로 저장을 사용하세요."
                                )
                            while chunk := source.read(1024 * 1024):
                                if canceled():
                                    raise AttachmentCanceledError(
                                        "드래그 복사 준비를 취소했습니다."
                                    )
                                size += len(chunk)
                                if used + total + size > self.MAX_BYTES:
                                    raise AttachmentOperationError(
                                        "드래그 사본 용량 한도를 초과했습니다."
                                    )
                                output.write(chunk)
                                digest.update(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                            if file_identity(before) != file_identity(
                                os.fstat(source.fileno())
                            ) or file_identity(before) != file_identity(resolved.path.stat()):
                                raise AttachmentOperationError(
                                    "복사 중 파일이 변경되었습니다. 다시 선택하세요."
                                )
                        attachment = resolved.attachment
                        if (
                            attachment
                            and attachment.is_frozen
                            and (
                                size != attachment.fixed_size_bytes
                                or digest.hexdigest() != attachment.fixed_checksum
                            )
                        ):
                            raise AttachmentOperationError("고정 자료의 내용이 일치하지 않습니다.")
                        with target.open("rb") as check:
                            verification = hashlib.sha256()
                            while chunk := check.read(1024 * 1024):
                                if canceled():
                                    raise AttachmentCanceledError(
                                        "드래그 복사 준비를 취소했습니다."
                                    )
                                verification.update(chunk)
                            if verification.hexdigest() != digest.hexdigest():
                                raise AttachmentOperationError("드래그 사본 검증에 실패했습니다.")
                        paths.append(target)
                        total += size
                    finally:
                        pin.__exit__(None, None, None)
                if canceled():
                    raise AttachmentCanceledError("드래그 복사 준비를 취소했습니다.")
            batch = DragCopyBatch(directory, tuple(paths), total)
            self._batches.append(batch)
            return batch
        except BaseException:
            self._remove(directory)
            raise

    def close(self) -> None:
        # Offered files remain available to delayed shell readers after close.
        if self._lease is not None:
            self._lease.__exit__(None, None, None)
            self._lease = None
