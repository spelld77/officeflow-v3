"""One owned parent journal for all prepared/published snapshot assets."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from officeflow.application.attachment_documents import InternalRecoveryItem
from officeflow.application.attachments import AttachmentCanceledError, AttachmentOperationError
from officeflow.application.file_operations import FileOperationCoordinator
from officeflow.application.task_snapshots import SnapshotCommand, SnapshotSelection
from officeflow.domain.task_history import fingerprint
from officeflow.infrastructure.attachments.internal_storage import checked_managed_path
from officeflow.infrastructure.attachments.read_lease import file_identity


class SnapshotStorage:
    def __init__(
        self,
        root: Path,
        coordinator: FileOperationCoordinator,
        checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.root, self.coordinator = root, coordinator
        self.checkpoint = checkpoint or (lambda _stage: None)

    @staticmethod
    def owner(job_id: str) -> str:
        return "snapshot:" + job_id

    def folder(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("보관 작업 ID가 올바르지 않습니다.")
        return checked_managed_path(self.root, ".operations/snapshot-" + job_id)

    def write(self, payload: dict[str, Any]) -> None:
        folder = self.folder(payload["job_id"])
        temporary = folder / (uuid4().hex + ".tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, folder / "journal.json")

    def read(self, job_id: str) -> dict[str, Any]:
        self.folder(job_id)  # validate the ID before interpreting any relative path
        path = checked_managed_path(
            self.root, f".operations/snapshot-{job_id}/journal.json", file_required=True
        )
        if path.stat().st_size > 512 * 1024:
            raise ValueError("보관 작업 기록 크기를 확인하세요.")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("format_version") != 1
            or payload.get("operation_kind") != "task_snapshot"
            or payload.get("job_id") != job_id
        ):
            raise ValueError("지원하지 않는 보관 작업 기록입니다.")
        if (
            type(payload.get("task_id")) is not int
            or payload["task_id"] < 1
            or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("request", "")))
        ):
            raise ValueError("보관 작업의 요청 정보가 올바르지 않습니다.")
        try:
            raw = payload["command"]
            occurrence = raw.get("occurrence_start")
            command = SnapshotCommand(
                raw["job_id"],
                raw["task_id"],
                raw["label"],
                tuple(SnapshotSelection(item["kind"], item["id"]) for item in raw["selections"]),
                datetime.fromisoformat(occurrence) if occurrence else None,
                raw["documents_closed"],
            )
            if (
                command.job_id != job_id
                or command.task_id != payload["task_id"]
                or fingerprint(asdict(command)) != payload["request"]
            ):
                raise ValueError("보관 요청이 작업 기록과 다릅니다.")
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("보관 요청의 소유 정보를 확인하세요.") from error
        assets = payload.get("assets")
        if not isinstance(assets, list) or len(assets) > 100:
            raise ValueError("보관 작업의 자산 목록이 올바르지 않습니다.")
        identifiers: set[str] = set()
        for item in assets:
            if (
                not isinstance(item, dict)
                or not re.fullmatch(r"[0-9a-f]{32}", str(item.get("id", "")))
                or item["id"] in identifiers
            ):
                raise ValueError("보관 작업의 자산 식별자가 올바르지 않습니다.")
            identifiers.add(item["id"])
            if not re.fullmatch(r"[0-9a-f]{32}", str(item.get("source_asset", ""))):
                raise ValueError("보관 출처 자산이 올바르지 않습니다.")
            checked_managed_path(self.root, item["source"])
            if type(item.get("new")) is not bool:
                raise ValueError("보관 작업의 소유 정보가 올바르지 않습니다.")
            if item["new"]:
                suffix = Path(item["path"]).suffix
                if suffix and not re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix):
                    raise ValueError("보관 작업의 확장자를 확인하세요.")
                expected = f"frozen/{item['id'][:2]}/{item['id'][2:4]}/{item['id']}{suffix}"
                if (
                    item["path"] != expected
                    or item.get("part") != f".operations/snapshot-{job_id}/{item['id']}.part"
                ):
                    raise ValueError("보관 작업의 소유 경로가 일치하지 않습니다.")
                checked_managed_path(self.root, item["part"])
                if item["id"] == item["source_asset"] or item["path"] == item["source"]:
                    raise ValueError("새 보관 파일과 출처가 겹칩니다.")
            elif (
                item["id"] != item["source_asset"]
                or item["path"] != item["source"]
                or item.get("part") is not None
            ):
                raise ValueError("재사용 보관의 소유 정보가 다릅니다.")
            checked_managed_path(self.root, item["path"])
            if item.get("size") is not None and (type(item["size"]) is not int or item["size"] < 0):
                raise ValueError("보관 파일 크기가 올바르지 않습니다.")
            if item.get("checksum") is not None and not re.fullmatch(
                r"[0-9a-f]{64}", item["checksum"]
            ):
                raise ValueError("보관 작업 검증값이 올바르지 않습니다.")
            identity = item.get("identity")
            if identity is not None and (
                not isinstance(identity, list)
                or len(identity) != 4
                or any(type(value) is not int for value in identity)
            ):
                raise ValueError("보관 파일의 식별 정보가 올바르지 않습니다.")
        if payload.get("state") not in {"copying", "prepared", "published"}:
            raise ValueError("보관 작업 단계가 올바르지 않습니다.")
        if payload["state"] != "copying" and any(
            item.get("size") is None
            or item.get("checksum") is None
            or (item["new"] and item.get("identity") is None)
            for item in assets
        ):
            raise ValueError("보관 준비 파일의 검증 정보가 없습니다.")
        return payload

    def protect_pending(self, job_id: str) -> InternalRecoveryItem:
        try:
            raw = self.read(job_id)
            paths = tuple(path for item in raw["assets"] for path in (item["source"], item["path"]))
            self.coordinator.protect(self.owner(job_id), paths)
            return InternalRecoveryItem(
                "snapshot-" + job_id,
                f"중단된 중요 보관 · 업무 {raw['task_id']} · 파일 {len(raw['assets'])}개 확인 필요",
            )
        except Exception as error:
            self.coordinator.protect(self.owner(job_id), None)
            return InternalRecoveryItem(
                "snapshot-" + job_id, f"보관 작업 자료를 보존했습니다: {error}"
            )

    @staticmethod
    def digest(
        stream: BinaryIO, canceled: Callable[[], bool], output: BinaryIO | None = None
    ) -> tuple[int, str]:
        stream.seek(0)
        start = file_identity(os.fstat(stream.fileno()))
        count, digest = 0, hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            if canceled():
                raise AttachmentCanceledError("보관을 취소했습니다. 현재 자료는 유지됩니다.")
            if output is not None:
                output.write(chunk)
            count += len(chunk)
            digest.update(chunk)
        if file_identity(os.fstat(stream.fileno())) != start:
            raise AttachmentOperationError("보관 중 문서가 변경되었습니다.")
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
        return count, digest.hexdigest()

    def finish(self, job_id: str) -> None:
        folder = self.folder(job_id)
        if not folder.exists():
            return
        if {item.name for item in folder.iterdir()} != {"journal.json"}:
            raise ValueError("보관 작업 폴더에 확인이 필요한 자료가 남았습니다.")
        (folder / "journal.json").unlink()
        folder.rmdir()
        self.coordinator.unprotect(self.owner(job_id))
