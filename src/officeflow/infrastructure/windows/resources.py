"""Read this process and system totals. No process enumeration or document data."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes as w
from time import monotonic

from officeflow.application.resource_policy import ResourceSnapshot


class _Performance(ctypes.Structure):
    _fields_ = (
        [("cb", w.DWORD)]
        + [
            (name, ctypes.c_size_t)
            for name in (
                "CommitTotal",
                "CommitLimit",
                "CommitPeak",
                "PhysicalTotal",
                "PhysicalAvailable",
                "SystemCache",
                "KernelTotal",
                "KernelPaged",
                "KernelNonpaged",
                "PageSize",
            )
        ]
        + [("HandleCount", w.DWORD), ("ProcessCount", w.DWORD), ("ThreadCount", w.DWORD)]
    )


class _Memory(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD)] + [
        (name, ctypes.c_size_t)
        for name in (
            "PeakWorkingSetSize",
            "WorkingSetSize",
            "QuotaPeakPagedPoolUsage",
            "QuotaPagedPoolUsage",
            "QuotaPeakNonPagedPoolUsage",
            "QuotaNonPagedPoolUsage",
            "PagefileUsage",
            "PeakPagefileUsage",
            "PrivateUsage",
        )
    ]


class WindowsResourceProbe:
    def __init__(self) -> None:
        self._handle: int | None = None
        self._closed = False
        if sys.platform != "win32":
            return
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self._user = ctypes.WinDLL("user32", use_last_error=True)
        for dll, name, args, result in (
            (self._kernel, "GetCurrentProcess", [], w.HANDLE),
            (self._kernel, "GetProcessHandleCount", [w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
            (self._kernel, "CreateMemoryResourceNotification", [ctypes.c_int], w.HANDLE),
            (
                self._kernel,
                "QueryMemoryResourceNotification",
                [w.HANDLE, ctypes.POINTER(w.BOOL)],
                w.BOOL,
            ),
            (self._kernel, "CloseHandle", [w.HANDLE], w.BOOL),
            (self._psapi, "GetPerformanceInfo", [ctypes.POINTER(_Performance), w.DWORD], w.BOOL),
            (
                self._psapi,
                "GetProcessMemoryInfo",
                [w.HANDLE, ctypes.POINTER(_Memory), w.DWORD],
                w.BOOL,
            ),
            (self._user, "GetGuiResources", [w.HANDLE, w.DWORD], w.DWORD),
        ):
            function = getattr(dll, name)
            function.argtypes, function.restype = args, result
        self._handle = self._kernel.CreateMemoryResourceNotification(0) or None

    def sample(self) -> ResourceSnapshot:
        now = monotonic()
        if self._closed or sys.platform != "win32":
            return ResourceSnapshot(now)
        performance, memory = _Performance(), _Memory()
        performance.cb, memory.cb = ctypes.sizeof(performance), ctypes.sizeof(memory)
        process = self._kernel.GetCurrentProcess()
        perf_ok = self._psapi.GetPerformanceInfo(ctypes.byref(performance), performance.cb)
        mem_ok = self._psapi.GetProcessMemoryInfo(process, ctypes.byref(memory), memory.cb)
        handles, low = w.DWORD(), w.BOOL()
        handles_ok = self._kernel.GetProcessHandleCount(process, ctypes.byref(handles))
        low_ok = self._handle and self._kernel.QueryMemoryResourceNotification(
            self._handle, ctypes.byref(low)
        )
        page = performance.PageSize
        return ResourceSnapshot(
            now,
            memory.WorkingSetSize if mem_ok else None,
            memory.PrivateUsage if mem_ok else None,
            handles.value if handles_ok else None,
            self._user.GetGuiResources(process, 0) or None,
            self._user.GetGuiResources(process, 1) or None,
            performance.CommitTotal * page if perf_ok and page else None,
            performance.CommitLimit * page if perf_ok and page else None,
            performance.PhysicalAvailable * page if perf_ok and page else None,
            performance.PhysicalTotal * page if perf_ok and page else None,
            bool(low.value) if low_ok else None,
        )

    def close(self) -> None:
        if self._handle is not None:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
        self._closed = True
