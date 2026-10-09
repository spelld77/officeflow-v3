"""Windows read sharing only: capture refuses concurrent writers/deleters."""

import ctypes
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO


@contextmanager
def protected_read(path: Path) -> Iterator[BinaryIO]:
    with _read_handle(path, access=0x80000000, sharing=1) as stream:
        yield stream


@contextmanager
def exclusive_read(path: Path) -> Iterator[BinaryIO]:
    """Read+DELETE rights, no sharing; keep this handle across verify/rename/delete."""
    with _read_handle(path, access=0x80010000, sharing=0) as stream:
        yield stream


@contextmanager
def _read_handle(path: Path, *, access: int, sharing: int) -> Iterator[BinaryIO]:
    if os.name != "nt":
        raise OSError("안정적인 파일 캡처는 Windows 읽기 보호가 필요합니다.")
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    handle = create(str(path), access, sharing, None, 3, 0x08000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close(handle)
        raise
    with os.fdopen(fd, "rb") as stream:
        yield stream


def rename_open_file(stream: BinaryIO, destination: Path) -> None:
    """Rename the verified file, not a potentially replaced path. No overwrite."""
    if os.name != "nt":
        raise OSError("핸들 기반 파일 이동은 Windows가 필요합니다.")
    import msvcrt
    from ctypes import wintypes

    class RenameInfo(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BOOLEAN),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    encoded = str(destination).encode("utf-16-le")
    buffer = ctypes.create_string_buffer(RenameInfo.FileName.offset + len(encoded) + 2)
    info = ctypes.cast(buffer, ctypes.POINTER(RenameInfo)).contents
    info.ReplaceIfExists = False
    info.RootDirectory = None
    info.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.FileName.offset, encoded, len(encoded))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    rename = kernel.SetFileInformationByHandle
    rename.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    rename.restype = wintypes.BOOL
    if not rename(msvcrt.get_osfhandle(stream.fileno()), 3, buffer, len(buffer)):
        raise ctypes.WinError(ctypes.get_last_error())


def delete_open_file(stream: BinaryIO) -> None:
    """Delete disposition on the same exclusively verified file; no path fallback."""
    if os.name != "nt":
        raise OSError("핸들 기반 파일 정리는 Windows가 필요합니다.")
    import msvcrt
    from ctypes import wintypes

    class Disposition(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    dispose = kernel.SetFileInformationByHandle
    dispose.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    dispose.restype = wintypes.BOOL
    info = Disposition(True)
    if not dispose(
        msvcrt.get_osfhandle(stream.fileno()), 4, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def file_identity(stat: os.stat_result) -> tuple[int, int, int, int]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
