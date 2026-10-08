"""Measure the actual streaming adapter in an isolated Windows process."""

import json
import shutil
import subprocess
import sys

import pytest


def test_one_gib_export_has_bounded_peak_rss(tmp_path):
    if shutil.disk_usage(tmp_path).free < 3 * 1024**3:
        pytest.skip("1 GiB fixture needs 3 GiB temporary free space")
    code = """
import ctypes, json, time, sys
from ctypes import wintypes
from pathlib import Path
from officeflow.application.attachment_export import FileReference, ResolvedFileReference
from officeflow.infrastructure.attachments.export_storage import ExternalExportStorage
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
data = root / "data"
data.mkdir()
source = root / "large.bin"
with source.open("wb") as stream:
    stream.seek(1024**3 - 1)
    stream.write(b"x")
storage = ExternalExportStorage(data)
reference = ResolvedFileReference(FileReference.orphan("fixture.bin"), source, source.name, "fixture.bin")
before, peak_before = memory()
start = time.monotonic()
prepared = storage.prepare(reference, root / "copy.bin", storage.new_job_id(), cancel_requested=lambda: False, progress=None)
output, warning = storage.publish(prepared, numbered=False, cancel_requested=lambda: False, confirm_overwrite=None)
after, peak_after = memory()
print(json.dumps({"size": output.stat().st_size, "rss_before": before, "rss_after": after, "rss_peak": peak_after, "peak_growth": peak_after - peak_before, "seconds": round(time.monotonic() - start, 3), "warning": warning}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert result["size"] == 1024**3
    assert result["peak_growth"] < 64 * 1024**2
    assert result["rss_after"] - result["rss_before"] < 32 * 1024**2
    assert not result["warning"]
    print("STREAMING_MEMORY " + json.dumps(result))
