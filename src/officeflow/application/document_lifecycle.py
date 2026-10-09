"""Shared lifecycle facade for record/calendar, search and data-management callers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from officeflow.application.attachment_deletion import (
    AttachmentDeletionService,
    DeleteCommand,
    DeletePlan,
    DeleteResult,
    DeleteSelection,
    DeleteTarget,
)
from officeflow.application.attachment_documents import (
    AttachmentDocumentService,
    DocumentCommand,
    DocumentOperation,
    DocumentResult,
)
from officeflow.domain.attachment import Attachment
from officeflow.infrastructure.attachments.view_cache import AttachmentViewCache
from officeflow.infrastructure.database.document_catalog import DocumentCatalog, DocumentEntry


class AttachmentAccess(Protocol):
    storage_root: Path

    def get_metadata(self, identity: int) -> Attachment: ...
    def path_for_open(self, identity: int) -> Path: ...


class DocumentLifecycle:
    def __init__(
        self,
        documents: AttachmentDocumentService,
        deletion: AttachmentDeletionService,
        catalog: DocumentCatalog,
        cache: AttachmentViewCache,
        changed: Callable[[int], None] = lambda _task_id: None,
    ) -> None:
        self.documents, self.deletion, self.catalog, self.cache = (
            documents,
            deletion,
            catalog,
            cache,
        )
        self.changed = changed

    def create(
        self,
        entry: DocumentEntry,
        operation: DocumentOperation,
        *,
        filename: str = "",
        label: str = "",
        memo: str = "",
        job_id: str | None = None,
        canceled: Callable[[], bool] = lambda: False,
    ) -> DocumentResult:
        command = DocumentCommand(
            job_id or uuid4().hex,
            operation,
            entry.kind,
            entry.id,
            entry.revision,
            filename,
            label,
            memo,
        )
        pending = self.cache.root / ".operations" / command.job_id / "journal.json"
        result = (
            self.documents.retry_pending(command, canceled)
            if pending.exists()
            else self.documents.execute(command, cancel_requested=canceled)
        )
        self.changed(entry.task_id)
        return result

    def prepare_open(
        self, kind: str, identity: int, canceled: Callable[[], bool] = lambda: False
    ) -> Path:
        entry = self.catalog.get(kind, identity)
        if entry.file.is_frozen:
            return self.cache.prepare(entry.file, canceled)
        from officeflow.infrastructure.attachments.internal_storage import checked_managed_path

        self.documents.coordinator.assert_accessible(entry.file.relative_path)
        return checked_managed_path(self.cache.root, entry.file.relative_path, file_required=True)

    def launch_failed(self, path: Path) -> None:
        copy = self.cache.by_path(path)
        if copy:
            self.cache.confirm_closed(copy, copy.use_token)

    def delete(
        self,
        plan: DeletePlan,
        *,
        include_versions: bool = False,
        canceled: Callable[[], bool] = lambda: False,
    ) -> DeleteResult:
        task_id = (
            plan.target.id
            if plan.target.kind in {"task", "selection"}
            else self.catalog.get(
                "version" if plan.target.kind == "version" else "attachment", plan.target.id
            ).task_id
        )
        result = self.deletion.execute(
            DeleteCommand(uuid4().hex, plan.target, plan.fingerprint, include_versions),
            cancel_requested=canceled,
        )
        self.changed(task_id)
        return result

    def plan(self, kind: str, identity: int) -> DeletePlan:
        return self.deletion.plan(DeleteTarget(kind, identity))

    def plan_many(self, task_id: int, targets: tuple[DeleteTarget, ...]) -> DeletePlan:
        return self.deletion.plan(DeleteSelection(task_id, targets))
