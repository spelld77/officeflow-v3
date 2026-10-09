"""Disposable views are not attachments. JSON owns each copy; the index is only a hint."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic, time
from typing import Any, BinaryIO
from uuid import uuid4

from officeflow.application.attachment_documents import (
    AttachmentDocumentService,
    DocumentCommand,
    DocumentOperation,
    DocumentResult,
)
from officeflow.application.attachments import AttachmentCanceledError, AttachmentOperationError
from officeflow.application.file_operations import FileOperationCoordinator
from officeflow.domain.attachment import Attachment, validate_display_name
from officeflow.infrastructure.attachments.internal_storage import checked_managed_path
from officeflow.infrastructure.attachments.read_lease import (
    delete_open_file,
    exclusive_read,
    file_identity,
    protected_read,
)

_ID = re.compile(r"[0-9a-f]{32}")
_HASH = re.compile(r"[0-9a-f]{64}")


def digest(stream: BinaryIO, canceled: Callable[[], bool] = lambda: False) -> tuple[int, str]:
    checksum, size = hashlib.sha256(), 0
    while block := stream.read(1024 * 1024):
        if canceled():
            raise AttachmentCanceledError("파일 확인을 취소했습니다.")
        checksum.update(block)
        size += len(block)
    return size, checksum.hexdigest()


def decode_cache_source(payload: str) -> dict[str, Any]:
    info = json.loads(payload)
    if not isinstance(info, dict) or set(info) != {
        "cache_id",
        "generation",
        "asset_id",
        "task_id",
        "family_id",
        "name",
        "relative_path",
        "size",
        "checksum",
        "identity",
    }:
        raise ValueError("보기 사본 출처 형식이 올바르지 않습니다.")
    for key in ("cache_id", "generation", "asset_id", "family_id"):
        if not isinstance(info[key], str) or not _ID.fullmatch(info[key]):
            raise ValueError("보기 사본 식별 정보가 올바르지 않습니다.")
    if (
        type(info["task_id"]) is not int
        or info["task_id"] <= 0
        or type(info["size"]) is not int
        or info["size"] < 0
    ):
        raise ValueError("보기 사본 크기/업무 정보가 올바르지 않습니다.")
    validate_display_name(info["name"])
    if not isinstance(info["checksum"], str) or not _HASH.fullmatch(info["checksum"]):
        raise ValueError("보기 사본 검증 정보가 올바르지 않습니다.")
    expected = f"cache/attachment-views/{info['generation']}/{info['cache_id']}/{info['name']}"
    if (
        info["relative_path"] != expected
        or len(info["identity"]) != 4
        or any(type(n) is not int for n in info["identity"])
    ):
        raise ValueError("보기 사본 소유 경로가 올바르지 않습니다.")
    return info


@dataclass(frozen=True, slots=True)
class ViewCopy:
    cache_id: str
    generation: str
    asset_id: str
    task_id: int
    family_id: str
    name: str
    relative_path: str
    baseline_size: int
    baseline_checksum: str
    last_used: float
    closed: bool
    state: str = "normal"
    observation: str = ""
    preserve_command: str = ""
    use_token: str = ""


@dataclass(frozen=True, slots=True)
class AssetCacheStatus:
    asset_id: str
    generation: str
    state: str


class AttachmentViewCache:
    def __init__(self, root: Path, coordinator: FileOperationCoordinator) -> None:
        self.root, self.coordinator = root, coordinator
        self.base = checked_managed_path(root, "cache/attachment-views")
        self.base.mkdir(parents=True, exist_ok=True)
        marker = self.base / "generation.json"
        if not marker.exists():
            self._write(marker, {"generation": uuid4().hex})
        raw = self._json(marker)
        self.generation = str(raw.get("generation", ""))
        if not _ID.fullmatch(self.generation):
            raise AttachmentOperationError(
                "보기 사본 세대 원장 점검이 필요합니다. 파일을 보존했습니다."
            )
        self.index = self.base / "index.sqlite"
        self._repair_iter: Any = None
        self._repair_generation = ""
        self._repair_dirs: Any = None
        self._ready = False  # also repair a crash between authoritative JSON and hint update
        try:
            with self._connection() as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("cache index")
        except sqlite3.DatabaseError:
            # Never treat a broken index as evidence that its copies don't exist.
            self.index.replace(self.base / f"index-corrupt-{uuid4().hex}.sqlite")
            self._ready = False
            with self._connection():
                pass

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.index, timeout=10)
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS copies (generation TEXT, id TEXT, asset TEXT, state TEXT, due REAL, used REAL, size INTEGER, PRIMARY KEY(generation,id))"
            )
            db.execute("CREATE INDEX IF NOT EXISTS candidate ON copies(due,id)")
            db.execute("CREATE INDEX IF NOT EXISTS source ON copies(generation,asset,id)")
            yield db
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _json(path: Path) -> dict[str, Any]:
        if path.stat().st_size > 32768:
            raise ValueError("보기 사본 원장이 너무 큽니다.")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("보기 사본 원장을 확인하세요.")
        return value

    @staticmethod
    def _write(path: Path, payload: object) -> None:
        part = path.with_name(f"view-write-{uuid4().hex}.part")
        with part.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(part, path)

    def _journal(self, generation: str, cache_id: str) -> Path:
        if not _ID.fullmatch(generation) or not _ID.fullmatch(cache_id):
            raise ValueError("보기 사본 ID가 올바르지 않습니다.")
        return checked_managed_path(
            self.root, f"cache/attachment-views/{generation}/{cache_id}/view.json"
        )

    def get(self, generation: str, cache_id: str) -> ViewCopy:
        return self._decode_copy(
            self._json(self._journal(generation, cache_id)), generation, cache_id
        )

    def _decode_copy(self, raw: dict[str, Any], generation: str, cache_id: str) -> ViewCopy:
        value = ViewCopy(**raw)
        if value.generation != generation or value.cache_id != cache_id:
            raise ValueError("보기 사본 소유 정보가 다릅니다.")
        decode_cache_source(
            json.dumps(
                {
                    "cache_id": cache_id,
                    "generation": generation,
                    "asset_id": value.asset_id,
                    "task_id": value.task_id,
                    "family_id": value.family_id,
                    "name": value.name,
                    "relative_path": value.relative_path,
                    "size": value.baseline_size,
                    "checksum": value.baseline_checksum,
                    "identity": [0, 0, 0, 0],
                }
            )
        )
        if type(value.closed) is not bool or value.state not in {
            "normal",
            "modified",
            "unavailable",
            "preparing",
        }:
            raise ValueError("보기 사본 상태가 올바르지 않습니다.")
        return value

    def _save(self, copy: ViewCopy, *, due: float | None = None) -> None:
        self._write(self._journal(copy.generation, copy.cache_id), asdict(copy))
        with self._connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO copies VALUES(?,?,?,?,?,?,?)",
                (
                    copy.generation,
                    copy.cache_id,
                    copy.asset_id,
                    copy.state,
                    time() + 3600 if due is None else due,
                    copy.last_used,
                    json.loads(copy.observation)["size"]
                    if copy.observation
                    else copy.baseline_size,
                ),
            )

    def repair_batch(self, limit: int = 20, canceled: Callable[[], bool] = lambda: False) -> bool:
        with self.coordinator.job(canceled, background=True):
            return self._repair_batch(limit, canceled)

    def _repair_batch(self, limit: int, canceled: Callable[[], bool]) -> bool:
        """Cursor survives batches in this process; crash restarts a bounded, safe rebuild."""
        if self._ready:
            return True
        if self._repair_dirs is None:
            self._repair_dirs = os.scandir(self.base)
        started, count = monotonic(), 0
        while count < limit and monotonic() - started < 2 and not canceled():
            if self._repair_iter is None:
                try:
                    entry = next(self._repair_dirs)
                except StopIteration:
                    self._repair_dirs.close()
                    self._repair_dirs = None
                    self._ready = True
                    return True
                count += 1
                if entry.name.startswith("discard-") and entry.is_file(follow_symlinks=False):
                    with suppress(OSError, ValueError, TypeError):
                        self._resume_discard(Path(entry.path))
                    continue
                if not _ID.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
                    continue
                self._repair_generation = entry.name
                self._repair_iter = os.scandir(entry.path)
            try:
                entry = next(self._repair_iter)
            except StopIteration:
                self._repair_iter.close()
                self._repair_iter = None
                continue
            count += 1
            if not _ID.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
                continue
            try:
                copy = self.get(self._repair_generation, entry.name)
                self._save(copy, due=time())
            except (OSError, ValueError, TypeError):
                # Unknown JSON/folders stay on disk and are never cleaned by the index.
                continue
        return False

    def page(
        self, offset: int = 0, limit: int = 100, *, asset_id: str | None = None
    ) -> tuple[ViewCopy, ...]:
        with self._connection() as db:
            keys = db.execute(
                "SELECT generation,id FROM copies "
                + ("WHERE asset=? " if asset_id else "")
                + "ORDER BY used DESC,id LIMIT ? OFFSET ?",
                (*((asset_id,) if asset_id else ()), min(limit, 100), offset),
            ).fetchall()
        copies = []
        for generation, cache_id in keys:
            try:
                copies.append(self.get(generation, cache_id))
            except (OSError, ValueError, TypeError):
                continue
        return tuple(copies)

    def observe(self, copy: ViewCopy, canceled: Callable[[], bool] = lambda: False) -> ViewCopy:
        with self.coordinator.job(canceled), self.coordinator.pin(copy.relative_path):
            return self._observe(copy, canceled)

    def _observe(self, copy: ViewCopy, canceled: Callable[[], bool]) -> ViewCopy:
        from dataclasses import replace

        copy = self.get(copy.generation, copy.cache_id)
        path = checked_managed_path(self.root, copy.relative_path, file_required=True)
        with protected_read(path) as stream:
            identity = file_identity(os.fstat(stream.fileno()))
            size, checksum = digest(stream, canceled)
            if file_identity(path.stat()) != identity:
                raise AttachmentOperationError("문서가 바뀌었습니다. 저장하고 닫은 뒤 확인하세요.")
        observation = json.dumps(
            {"size": size, "checksum": checksum, "identity": identity}, sort_keys=True
        )
        state = (
            "normal"
            if (size, checksum) == (copy.baseline_size, copy.baseline_checksum)
            else "modified"
        )
        if copy.state == "preparing" and state != "normal":
            state = "preparing"  # partial capture is not a saved edit of a complete document
        result = replace(copy, observation=observation, state=state)
        self._save(result)
        return result

    def prepare(self, attachment: Attachment, canceled: Callable[[], bool]) -> Path:
        """Verify frozen source even when a reusable copy exists. Never expose source."""
        from dataclasses import replace

        if not attachment.is_frozen:
            raise ValueError("고정 자료만 보기 사본을 사용합니다.")
        with self.coordinator.job(canceled), self.coordinator.pin(attachment.relative_path):
            source = checked_managed_path(self.root, attachment.relative_path, file_required=True)
            with protected_read(source) as stream:
                size, checksum = digest(stream, canceled)
                if (size, checksum) != (attachment.fixed_size_bytes, attachment.fixed_checksum):
                    raise AttachmentOperationError(
                        "보관 후 내용이 변경되었습니다. 기준을 바꾸지 않았습니다."
                    )
                # Rebuild incrementally; creation waits until ownership inventory is safe.
                if not self.repair_batch(100, canceled):
                    raise AttachmentOperationError(
                        "보기 사본 목록을 준비 중입니다. 잠시 후 다시 열어주세요."
                    )
                with self._connection() as db:
                    keys = db.execute(
                        "SELECT id FROM copies WHERE generation=? AND asset=? ORDER BY used DESC LIMIT 4",
                        (self.generation, attachment.asset_id),
                    ).fetchall()
                    totals = db.execute(
                        "SELECT count(*),coalesce(sum(size),0) FROM copies WHERE state='normal'"
                    ).fetchone()
                protected = 0
                for (cache_id,) in keys:
                    try:
                        copy = self.observe(self.get(self.generation, cache_id), canceled)
                        if copy.state == "normal":
                            # Token recorded before caller launches the editor.
                            copy = replace(
                                copy, last_used=time(), closed=False, use_token=uuid4().hex
                            )
                            self._save(copy)
                            return checked_managed_path(
                                self.root, copy.relative_path, file_required=True
                            )
                    except (OSError, ValueError, AttachmentOperationError):
                        pass
                    protected += 1
                if (
                    protected >= 2
                    or totals[0] >= 100
                    or (totals[1] + size > 512 * 1024 * 1024 and totals[0] > 0)
                ):
                    raise AttachmentOperationError(
                        "보호/보기 사본이 많습니다. 데이터 → 보기 사본에서 보존·닫음 확인 후 정리하세요."
                    )
                cache_id = uuid4().hex
                name = validate_display_name(attachment.name)
                relative = f"cache/attachment-views/{self.generation}/{cache_id}/{name}"
                path = checked_managed_path(self.root, relative)
                path.parent.mkdir(parents=True, exist_ok=False)
                copy = ViewCopy(
                    cache_id,
                    self.generation,
                    attachment.asset_id,
                    attachment.task_id,
                    attachment.family_id,
                    name,
                    relative,
                    size,
                    checksum,
                    time(),
                    False,
                    "preparing",
                )
                self._save(copy)
                stream.seek(0)
                with path.open("xb") as output:
                    while block := stream.read(1024 * 1024):
                        if canceled():
                            raise AttachmentCanceledError(
                                "보기 사본 생성을 취소했습니다. 준비 자료는 보존했습니다."
                            )
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                copy = self.observe(copy, canceled)
                self._save(replace(copy, use_token=uuid4().hex))
                return path

    def by_path(self, path: Path) -> ViewCopy | None:
        try:
            relative = path.relative_to(self.base)
            if len(relative.parts) != 3:
                return None
            return self.get(relative.parts[0], relative.parts[1])
        except (ValueError, OSError, TypeError):
            return None

    def confirm_closed(self, copy: ViewCopy, token: str) -> ViewCopy:
        with self.coordinator.job():
            return self._confirm_closed(copy, token)

    def _confirm_closed(self, copy: ViewCopy, token: str) -> ViewCopy:
        from dataclasses import replace

        current = self.get(copy.generation, copy.cache_id)
        if current.use_token != token:
            raise ValueError("사본을 다시 열었습니다. 닫음 여부를 다시 확인하세요.")
        current = self.observe(current)
        current = replace(current, closed=True)
        self._save(current, due=time())
        return current

    def _checkpoint(self, stage: str) -> None:
        """Crash-test boundary; never a reason to replay a file deletion."""

    def _discard_marker(self, copy: ViewCopy) -> Path:
        return self.base / f"discard-{copy.generation}-{copy.cache_id}.json"

    def _resume_discard(self, marker: Path) -> bool:
        raw = self._json(
            checked_managed_path(
                self.root, marker.relative_to(self.root).as_posix(), file_required=True
            )
        )
        copy = self._decode_copy(raw, str(raw.get("generation", "")), str(raw.get("cache_id", "")))
        if marker != self._discard_marker(copy) or not copy.closed or not copy.observation:
            raise ValueError("사본 정리 소유 기록이 올바르지 않습니다.")
        path = checked_managed_path(self.root, copy.relative_path)
        if path.exists():
            return False  # approved intent alone never authorizes automatic byte deletion
        journal = self._journal(copy.generation, copy.cache_id)
        if journal.exists() and self.get(copy.generation, copy.cache_id) != copy:
            raise ValueError("정리 도중 사본 기록이 변경되었습니다.")
        if path.parent.exists():
            if any(item.name != "view.json" for item in path.parent.iterdir()):
                raise ValueError("미분류 자료가 있어 정리를 보류했습니다.")
            journal.unlink(missing_ok=True)
            self._checkpoint("discard_metadata_removed")
            path.parent.rmdir()
        with self._connection() as db:
            db.execute(
                "DELETE FROM copies WHERE generation=? AND id=?", (copy.generation, copy.cache_id)
            )
        marker.unlink()  # intent stays durable until every bookkeeping step has finished
        return True

    def usage(self) -> tuple[int, int, int]:
        with self._connection() as db:
            count, size, protected = db.execute(
                "SELECT count(*),coalesce(sum(size),0),coalesce(sum(state!='normal'),0) FROM copies"
            ).fetchone()
        return int(count), int(size), int(protected)

    def discard(self, copy: ViewCopy, *, changed_consent: bool = False) -> None:
        with self.coordinator.job():
            self._discard(copy, changed_consent=changed_consent)

    def discard_after_close(
        self,
        copy: ViewCopy,
        *,
        closed_consent: bool,
        canceled: Callable[[], bool] = lambda: False,
    ) -> None:
        """One explicit consent, without approving bytes changed after the dialog opened."""
        from dataclasses import replace

        if not closed_consent or copy.generation != self.generation:
            raise ValueError("현재 사본의 문서를 닫았음을 확인해 주세요.")
        with self.coordinator.job(canceled):
            current = self.get(copy.generation, copy.cache_id)
            if current.use_token != copy.use_token:
                raise ValueError(
                    "사본을 다시 열었습니다. 문서를 닫은 뒤 확인 창을 닫고 '변경 버리기/정리'를 다시 선택하세요."
                )
            current = self.observe(current, canceled)
            if not copy.observation or current.observation != copy.observation:
                raise ValueError(
                    "확인 뒤 문서가 바뀌어 버리지 않았습니다. 확인 창을 닫고 '변경 버리기/정리'를 다시 선택하세요."
                )
            current = replace(current, closed=True)
            self._save(current, due=time())
            self._discard(current, changed_consent=True, canceled=canceled)

    def _discard(
        self,
        copy: ViewCopy,
        *,
        changed_consent: bool,
        canceled: Callable[[], bool] = lambda: False,
    ) -> None:
        current = self.get(copy.generation, copy.cache_id)
        if (
            current.generation != self.generation
            or not current.closed
            or (current.state == "preparing" and not changed_consent)
        ):
            raise ValueError("현재 세대의 문서를 저장하고 닫음 확인 후 정리하세요.")
        if (
            not copy.observation
            or copy.observation != current.observation
            or copy.use_token != current.use_token
        ):
            raise ValueError("사본 확인 결과가 오래되었습니다. 다시 확인하세요.")
        path = checked_managed_path(self.root, current.relative_path, file_required=True)
        with self.coordinator.pin(current.relative_path), exclusive_read(path) as stream:
            identity = file_identity(os.fstat(stream.fileno()))
            size, checksum = digest(stream, canceled)
            expected = json.loads(copy.observation)
            if (size, checksum, list(identity)) != (
                expected["size"],
                expected["checksum"],
                expected["identity"],
            ):
                raise ValueError("확인 뒤 문서가 바뀌어 정리하지 않았습니다.")
            if (size, checksum) != (
                current.baseline_size,
                current.baseline_checksum,
            ) and not changed_consent:
                raise ValueError("변경된 사본은 작업본 보존 또는 변경 내용 버리기를 선택하세요.")
            if set(p.name for p in path.parent.iterdir()) != {path.name, "view.json"}:
                raise ValueError("편집기 임시 파일이 있어 정리를 보류했습니다.")
            if canceled():
                raise AttachmentCanceledError("사본 정리를 취소했습니다.")
            marker = self._discard_marker(current)
            self._write(marker, asdict(current))
            self._checkpoint("discard_approved")
            if canceled():
                raise AttachmentCanceledError("사본 정리를 취소했습니다.")
            delete_open_file(stream)
        self._checkpoint("discard_file_deleted")
        self._resume_discard(marker)

    def preserve(
        self,
        copy: ViewCopy,
        filename: str,
        documents: AttachmentDocumentService,
        canceled: Callable[[], bool],
    ) -> DocumentResult:
        with self.coordinator.job(canceled):
            return self._preserve(copy, filename, documents, canceled)

    def _preserve(
        self,
        copy: ViewCopy,
        filename: str,
        documents: AttachmentDocumentService,
        canceled: Callable[[], bool],
    ) -> DocumentResult:
        from dataclasses import replace

        current = self.get(copy.generation, copy.cache_id)
        if current.generation != self.generation or not current.closed:
            raise ValueError("문서를 저장하고 닫음 확인 후 작업본으로 보존하세요.")
        current = self.observe(current, canceled)
        if current.state == "preparing":
            raise ValueError(
                "생성이 끝나지 않은 사본입니다. 필요한 내용은 외부 저장으로 따로 보존하세요."
            )
        if current.preserve_command:
            command = DocumentCommand(
                **{**json.loads(current.preserve_command), "operation": DocumentOperation.WORK}
            )
            committed = documents.repository.committed(command)
            if committed is None and command.filename != filename:
                raise ValueError("보존 요청이 진행 중입니다. 같은 이름으로 다시 시도하세요.")
            previous = decode_cache_source(command.cache_payload)
            now = json.loads(current.observation)
            if committed is not None and (now["size"], now["checksum"]) != (
                previous["size"],
                previous["checksum"],
            ):
                # A later explicit click preserves later saved edits as a new independent work.
                current = replace(current, preserve_command="")
                self._save(current)
        if not current.preserve_command:
            obs = json.loads(current.observation)
            payload = json.dumps(
                {
                    "cache_id": current.cache_id,
                    "generation": current.generation,
                    "asset_id": current.asset_id,
                    "task_id": current.task_id,
                    "family_id": current.family_id,
                    "name": current.name,
                    "relative_path": current.relative_path,
                    **obs,
                },
                sort_keys=True,
            )
            command = DocumentCommand(
                uuid4().hex, DocumentOperation.WORK, "cache", 1, 0, filename, cache_payload=payload
            )
            self._save(
                replace(current, preserve_command=json.dumps(asdict(command), ensure_ascii=False))
            )
        pending = self.root / ".operations" / command.job_id / "journal.json"
        result = (
            documents.retry_pending(command, canceled)
            if pending.exists()
            else documents.execute(command, cancel_requested=canceled)
        )
        try:
            # A committed receipt is idempotent, but later copy edits are never discarded.
            expected = decode_cache_source(command.cache_payload)
            observed = self.observe(self.get(current.generation, current.cache_id), canceled)
            value = json.loads(observed.observation)
            if (value["size"], value["checksum"], value["identity"]) == (
                expected["size"],
                expected["checksum"],
                expected["identity"],
            ):
                self.discard(observed, changed_consent=True)
            else:
                return replace(
                    result, warning="작업본은 보존했습니다. 이후 바뀐 보기 사본은 남겨두었습니다."
                )
        except Exception:
            return replace(result, warning="작업본은 보존했습니다. 보기 사본 정리는 보류했습니다.")
        return result

    def maintenance(self, canceled: Callable[[], bool] = lambda: False) -> int:
        if not self.repair_batch(20, canceled):
            return 0
        with self._connection() as db:
            keys = db.execute(
                "SELECT generation,id FROM copies WHERE due<=? ORDER BY due,id LIMIT 20", (time(),)
            ).fetchall()
        started, removed = monotonic(), 0
        for generation, cache_id in keys:
            if canceled() or monotonic() - started > 2:
                break
            with self._connection() as db:
                db.execute(
                    "UPDATE copies SET due=? WHERE generation=? AND id=?",
                    (time() + 3600, generation, cache_id),
                )
            try:
                copy = self.get(generation, cache_id)
                self._save(copy, due=time() + 3600)  # failures cannot starve the next page
                if (
                    generation != self.generation
                    or not copy.closed
                    or time() - copy.last_used < 7 * 86400
                    or copy.state == "preparing"
                ):
                    continue
                with self.coordinator.job(canceled, background=True):
                    copy = self.observe(copy, canceled)
                    if copy.state == "normal":
                        self.discard(copy)
                        removed += 1
            except Exception:
                continue
        return removed

    def check_assets(
        self, assets: tuple[str, ...], canceled: Callable[[], bool]
    ) -> tuple[AssetCacheStatus, ...]:
        ids = tuple(dict.fromkeys(assets))[:100]
        if not ids:
            return ()
        with self._connection() as db:
            existing = {
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT asset FROM copies WHERE generation=? AND asset IN ("
                    + ",".join("?" for _id in ids)
                    + ")",
                    (self.generation, *ids),
                ).fetchall()
            }
            keys = db.execute(
                "SELECT id FROM copies WHERE generation=? AND asset IN ("
                + ",".join("?" for _id in ids)
                + ") ORDER BY used DESC LIMIT 100",
                (self.generation, *ids),
            ).fetchall()
        results = {
            asset: AssetCacheStatus(
                asset, self.generation, "unavailable" if asset in existing else "none"
            )
            for asset in ids
        }
        for (cache_id,) in keys:
            if canceled():
                break
            try:
                with self.coordinator.job(canceled, background=True):
                    copy = self.observe(self.get(self.generation, cache_id), canceled)
                if results[copy.asset_id].state != "modified" or copy.state == "modified":
                    results[copy.asset_id] = AssetCacheStatus(
                        copy.asset_id, self.generation, copy.state
                    )
            except (OSError, ValueError, AttachmentOperationError):
                continue
        return tuple(results.values())

    def close(self) -> None:
        for cursor in (self._repair_iter, self._repair_dirs):
            if cursor is not None:
                cursor.close()
