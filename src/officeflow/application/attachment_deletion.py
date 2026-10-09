"""Confirmed logical deletion, recoverable quarantine, then physical cleanup."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field, replace
from typing import Protocol

from officeflow.application.attachment_documents import fingerprint
from officeflow.application.attachments import AttachmentOperationError
from officeflow.application.file_operations import FileJobCanceledError, FileOperationCoordinator


@dataclass(frozen=True, slots=True)
class DeleteTarget:
    kind: str
    id: int

    def __post_init__(self) -> None:
        if (
            self.kind not in {"attachment", "version", "task", "snapshot"}
            or type(self.id) is not int
            or self.id < 1
        ):
            raise ValueError("삭제 대상이 올바르지 않습니다.")


@dataclass(frozen=True, slots=True)
class DeleteSelection:
    id: int  # Owning task, not permission to delete that task.
    items: tuple[DeleteTarget, ...]
    kind: str = field(default="selection", init=False)

    def __post_init__(self) -> None:
        if (
            type(self.id) is not int
            or self.id < 1
            or not isinstance(self.items, tuple)
            or not 1 <= len(self.items) <= 100
            or any(
                not isinstance(item, DeleteTarget) or item.kind not in {"attachment", "version"}
                for item in self.items
            )
            or len(set(self.items)) != len(self.items)
        ):
            raise ValueError("같은 업무의 첨부/보관본을 중복 없이 1~100개 선택하세요.")


def decode_delete_target(raw: object) -> DeleteTarget | DeleteSelection:
    if not isinstance(raw, dict):
        raise ValueError("삭제 대상 형식이 올바르지 않습니다.")
    if set(raw) == {"kind", "id"}:
        return DeleteTarget(raw["kind"], raw["id"])
    if set(raw) == {"kind", "id", "items"} and raw["kind"] == "selection":
        if not isinstance(raw["items"], list) or not 1 <= len(raw["items"]) <= 100:
            raise ValueError("선택 삭제 대상 형식이 올바르지 않습니다.")
        if any(not isinstance(item, dict) or set(item) != {"kind", "id"} for item in raw["items"]):
            raise ValueError("선택 삭제에는 단일 첨부/보관본만 포함할 수 있습니다.")
        return DeleteSelection(
            raw["id"], tuple(DeleteTarget(item["kind"], item["id"]) for item in raw["items"])
        )
    raise ValueError("삭제 대상 형식이 올바르지 않습니다.")


@dataclass(frozen=True, slots=True)
class DeleteAsset:
    id: str
    path: str
    kind: str
    removable: bool

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", self.id) or self.kind not in {"mutable", "frozen"}:
            raise ValueError("삭제 자산 정보가 올바르지 않습니다.")
        if type(self.path) is not str or not self.path or type(self.removable) is not bool:
            raise ValueError("삭제 경로 정보가 올바르지 않습니다.")


@dataclass(frozen=True, slots=True)
class DeletePlan:
    target: DeleteTarget | DeleteSelection
    fingerprint: str
    assets: tuple[DeleteAsset, ...]
    attachment_count: int
    version_count: int
    kept_versions: int
    work_log_count: int
    history_count: int = 0
    snapshot_count: int = 0
    snapshot_file_count: int = 0

    @property
    def removable_count(self) -> int:
        return sum(asset.removable for asset in self.assets)

    @property
    def kept_asset_count(self) -> int:
        return len(self.assets) - self.removable_count


@dataclass(frozen=True, slots=True)
class DeleteCommand:
    job_id: str
    target: DeleteTarget | DeleteSelection
    plan_fingerprint: str
    include_versions: bool = False

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", self.job_id) or not re.fullmatch(
            r"[0-9a-f]{64}", self.plan_fingerprint
        ):
            raise ValueError("삭제 명령 식별자가 올바르지 않습니다.")
        if type(self.include_versions) is not bool:
            raise ValueError("보관본 삭제 범위 확인이 올바르지 않습니다.")
        if self.include_versions and self.target.kind != "task":
            raise ValueError("업무 전체 삭제에만 보관본 범위 확인을 사용합니다.")

    @property
    def request_fingerprint(self) -> str:
        return fingerprint(asdict(self))


@dataclass(frozen=True, slots=True)
class DeleteCommit:
    command: DeleteCommand
    assets: tuple[DeleteAsset, ...]
    attachment_count: int
    version_count: int
    work_log_count: int
    history_count: int = 0
    snapshot_count: int = 0
    snapshot_file_count: int = 0


@dataclass(frozen=True, slots=True)
class DeleteResult:
    job_id: str
    already_committed: bool = False
    pending: bool = False
    warning: str = ""


@dataclass(frozen=True, slots=True)
class DeleteRecoveryItem:
    job_id: str
    command: DeleteCommand | None
    message: str


@dataclass(frozen=True, slots=True)
class DeleteRecoveryReport:
    recovered: int
    pending: int
    warnings: tuple[str, ...]


class DeleteRepository(Protocol):
    def plan(self, target: DeleteTarget | DeleteSelection) -> DeletePlan: ...
    def committed(self, command: DeleteCommand) -> DeleteCommit | None: ...
    def finalize(self, command: DeleteCommand, plan: DeletePlan) -> DeleteCommit: ...
    def recovery_state(self, asset: DeleteAsset) -> str: ...
    def finish_asset(self, commit: DeleteCommit, asset: DeleteAsset) -> None: ...


class DeleteStorage(Protocol):
    coordinator: FileOperationCoordinator

    def prepare(
        self, command: DeleteCommand, plan: DeletePlan, canceled: Callable[[], bool]
    ) -> None: ...
    def assert_quarantined(
        self, command: DeleteCommand, assets: tuple[DeleteAsset, ...]
    ) -> None: ...
    def rollback(
        self,
        command: DeleteCommand,
        repository: DeleteRepository,
        *,
        canceled: Callable[[], bool] = lambda: False,
    ) -> None: ...
    def commit_guard(
        self, command: DeleteCommand, assets: tuple[DeleteAsset, ...]
    ) -> AbstractContextManager[None]: ...
    def finish(
        self,
        commit: DeleteCommit,
        repository: DeleteRepository,
        *,
        canceled: Callable[[], bool] = lambda: False,
    ) -> None: ...
    def protect_pending(self, job_id: str) -> None: ...
    def checkpoint(self, stage: str) -> None: ...


class AttachmentDeletionService:
    def __init__(self, repository: DeleteRepository, storage: DeleteStorage) -> None:
        self._repository = repository
        self._storage = storage
        self.coordinator = storage.coordinator

    def plan(self, target: DeleteTarget | DeleteSelection) -> DeletePlan:
        with self.coordinator.gate:
            plan = self._repository.plan(target)
            self.coordinator.assert_not_pinned(tuple(asset.path for asset in plan.assets))
            return plan

    def execute(
        self, command: DeleteCommand, *, cancel_requested: Callable[[], bool] | None = None
    ) -> DeleteResult:
        canceled = cancel_requested or (lambda: False)
        with self.coordinator.job(canceled):
            commit = self._repository.committed(command)
            if commit is not None:
                return self._finish(commit, already_committed=True, canceled=canceled)
            with self.coordinator.gate:
                plan = self._repository.plan(command.target)
                if plan.fingerprint != command.plan_fingerprint:
                    raise AttachmentOperationError(
                        "삭제할 자료가 변경되었습니다. 삭제 범위를 다시 확인하세요."
                    )
                if (
                    (plan.version_count or plan.history_count or plan.snapshot_count)
                    and command.target.kind == "task"
                    and not command.include_versions
                ):
                    raise AttachmentOperationError(
                        "보관본·업무 이력·중요 보관까지 삭제하는 범위를 별도로 확인해 주세요."
                    )
                intent = self.coordinator.deletion_intent(
                    tuple(asset.path for asset in plan.assets), owner="delete:" + command.job_id
                )
                intent.__enter__()
            try:
                self._storage.prepare(command, plan, canceled)
                if canceled():
                    raise FileJobCanceledError("삭제를 취소했습니다. 기존 자료를 복구합니다.")
                self._storage.checkpoint("quarantined")
                with self._storage.commit_guard(command, plan.assets), self.coordinator.gate:
                    commit = self._repository.finalize(command, plan)
                self._storage.checkpoint("committed")
                return self._finish(commit, canceled=canceled)
            except BaseException as error:
                self._storage.protect_pending(command.job_id)
                if isinstance(error, Exception):
                    # A failure *after* COMMIT is not permission to undo a deletion.
                    try:
                        commit = self._repository.committed(command)
                        if commit is not None:
                            return self._finish(commit, canceled=canceled)
                        # Cancel stops new deletion, not the compensating rollback.
                        # Restart/background reconciliation remains cancellable per file.
                        self._storage.rollback(command, self._repository)
                    except Exception as recovery_error:
                        raise AttachmentOperationError(
                            "삭제를 확정하지 못했거나 복구 확인이 필요합니다. 격리 자료를 보존했습니다."
                        ) from recovery_error
                raise
            finally:
                intent.__exit__(None, None, None)

    def _finish(
        self,
        commit: DeleteCommit,
        *,
        already_committed: bool = False,
        canceled: Callable[[], bool] = lambda: False,
    ) -> DeleteResult:
        result = DeleteResult(commit.command.job_id, already_committed=already_committed)
        try:
            self._storage.protect_pending(commit.command.job_id)
            self._storage.finish(commit, self._repository, canceled=canceled)
        except Exception as error:
            self._storage.protect_pending(commit.command.job_id)
            return replace(
                result,
                pending=True,
                warning="논리 삭제는 완료했지만 파일 정리는 보류했습니다: " + str(error),
            )
        return result

    def recover(
        self,
        items: tuple[DeleteRecoveryItem, ...],
        *,
        cancel_requested: Callable[[], bool] | None = None,
        limit: int = 20,
        background: bool = False,
    ) -> DeleteRecoveryReport:
        canceled = cancel_requested or (lambda: False)
        recovered = 0
        warnings: list[str] = []
        for item in items[: max(0, limit)]:
            if canceled():
                break
            if item.command is None:
                warnings.append(item.message)
                continue
            try:
                with self.coordinator.job(canceled, background=background):
                    self._storage.protect_pending(item.job_id)
                    commit = self._repository.committed(item.command)
                    if commit is None:
                        self._storage.rollback(item.command, self._repository, canceled=canceled)
                    else:
                        self._storage.finish(commit, self._repository, canceled=canceled)
                    recovered += 1
            except Exception as error:
                self._storage.protect_pending(item.job_id)
                warnings.append(f"삭제 복구 보류 ({item.job_id}): {error}")
        return DeleteRecoveryReport(recovered, len(items) - recovered, tuple(warnings))
