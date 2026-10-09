"""Measure the actual streaming adapter in an isolated Windows process."""

import json
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("mode", ["external", "internal", "view", "backup", "snapshot"])
def test_one_gib_export_has_bounded_peak_rss(tmp_path, mode):
    if shutil.disk_usage(tmp_path).free < 8 * 1024**3:
        pytest.skip("1 GiB full-backup fixture needs 8 GiB temporary free space")
    code = """
import ctypes, json, time, sys
from ctypes import wintypes
from pathlib import Path
from officeflow.application.attachment_export import FileReference, ResolvedFileReference
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
from officeflow.application.attachment_documents import AttachmentDocumentService, DocumentCommand, DocumentOperation
from officeflow.application.file_operations import FileOperationCoordinator
from officeflow.infrastructure.attachments.internal_storage import InternalFileStorage
from officeflow.infrastructure.database.attachment_document_repository import SqlAlchemyDocumentRepository
from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.models import AttachmentRecord, TaskRecord
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine
from datetime import UTC, datetime
from uuid import uuid4
from officeflow.bootstrap.paths import AppPaths
from officeflow.infrastructure.backup import BackupManager
from officeflow.infrastructure.attachments.view_cache import AttachmentViewCache
from officeflow.infrastructure.database.document_catalog import DocumentCatalog
from officeflow.infrastructure.database.task_snapshot_repository import TaskSnapshotService
from officeflow.infrastructure.attachments.snapshot_storage import SnapshotStorage
from officeflow.application.task_snapshots import SnapshotCommand, SnapshotSelection
class Counters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
def memory():
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    api = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    api.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    api.restype = wintypes.BOOL
    if not api(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb): raise ctypes.WinError(ctypes.get_last_error())
    return counters.WorkingSetSize, counters.PeakWorkingSetSize
root = Path(sys.argv[1])
mode = sys.argv[2]
data = root / "data"
data.mkdir()
if mode == 'backup':
    paths = AppPaths(root)
    paths.ensure_directories()
    data = paths.attachment_dir
source = root / "large.bin"
with source.open("wb") as stream:
    stream.seek(1024**3 - 1)
    stream.write(b"x")
mode = sys.argv[2]
if mode == "external":
    storage = ExternalExportStorage(data)
    reference = ResolvedFileReference(FileReference.orphan("fixture.bin"), source, source.name, "fixture.bin")
else:
    database = paths.database_file if mode == 'backup' else root / "officeflow.db"
    upgrade_database(database)
    engine = create_database_engine(database)
    sessions = SessionFactory(engine)
    now = datetime.now(UTC)
    source.rename(data / "large.bin")
    with sessions.transaction() as session:
        task = TaskRecord(title="isolated large file", created_at=now, updated_at=now)
        session.add(task)
        session.flush()
        attached = AttachmentRecord(task_id=task.id, original_name="large.bin", stored_name="large.bin", relative_path="large.bin", size_bytes=1024**3, checksum=None, created_at=now, sort_order=10)
        session.add(attached)
        session.flush()
        source_id = attached.id
    internal_repository = SqlAlchemyDocumentRepository(sessions)
    internal_storage = InternalFileStorage(data, coordinator=FileOperationCoordinator())
    service = AttachmentDocumentService(internal_repository, internal_storage)
    request = DocumentCommand(uuid4().hex, DocumentOperation.VERSION, "attachment", source_id, 0, label="large frozen fixture")
    internal_repository.source(request)
    if mode == 'view':
        result = service.execute(request)
        frozen = DocumentCatalog(sessions).get('version',result.version_id).file
        view_cache = AttachmentViewCache(data,internal_storage.coordinator)
    if mode == 'backup':
        manager = BackupManager(paths,coordinator=internal_storage.coordinator)
    if mode == 'snapshot':
        snapshot_service = TaskSnapshotService(sessions,SnapshotStorage(data,internal_storage.coordinator))
        snapshot_request = SnapshotCommand(uuid4().hex,task.id,'large bundle',(SnapshotSelection('attachment',source_id),),documents_closed=True)
before, peak_before = memory()
start = time.monotonic()
if mode == "external":
    prepared = storage.prepare(reference, root / "copy.bin", storage.new_job_id(), cancel_requested=lambda: False, progress=None)
    output, warning = storage.publish(prepared, numbered=False, cancel_requested=lambda: False, confirm_overwrite=None)
elif mode == 'internal':
    result = service.execute(request)
    output = data / internal_repository.committed(request).relative_path
    warning = result.warning
elif mode == 'view':
    output = view_cache.prepare(frozen,lambda:False)
    warning = ''
elif mode == 'snapshot':
    result = snapshot_service.create(snapshot_request)
    file = DocumentCatalog(sessions).snapshot_files(task.id,result.snapshot_id).items[0].file
    output = data / file.relative_path
    warning = result.warning
else:
    backup = manager.create_backup()
    manifest = manager.verify_backup(backup.path)
    assert sum(item.size for item in manifest.attachments)==1024**3
    output = data / 'large.bin'
    warning = ''
after, peak_after = memory()
print(json.dumps({"mode": mode, "size": output.stat().st_size, "rss_before": before, "rss_after": after, "rss_peak": peak_after, "peak_growth": peak_after - peak_before, "seconds": round(time.monotonic() - start, 3), "warning": warning}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), mode],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert result["size"] == 1024**3
    assert result["peak_growth"] < (64 if mode == "external" else 32) * 1024**2
    assert result["rss_after"] - result["rss_before"] < 32 * 1024**2
    assert not result["warning"]
    print("STREAMING_MEMORY " + json.dumps(result))
