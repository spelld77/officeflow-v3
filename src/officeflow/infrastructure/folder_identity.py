"""Bounded helper-process directory probes; no application or database startup."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCAN_LIMIT = 1000
SCAN_SECONDS = 2


def machine_key() -> str | None:
    if os.name != "nt":
        return None
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            value = str(winreg.QueryValueEx(key, "MachineGuid")[0])
        return hashlib.sha256(value.encode()).hexdigest()
    except OSError:
        return None


def directory_identity(path: Path) -> str | None:
    machine = machine_key()
    if machine is None or str(path).startswith("\\\\"):
        return None
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    drive_type = kernel.GetDriveTypeW
    drive_type.argtypes, drive_type.restype = [w.LPCWSTR], w.UINT
    if drive_type(path.anchor) != 3:  # DRIVE_FIXED only; remote/removable use ordinary paths.
        return None
    create = kernel.CreateFileW
    create.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
    create.restype = w.HANDLE
    handle = create(str(path), 0x80, 7, None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        return None
    close = kernel.CloseHandle
    close.argtypes = [w.HANDLE]

    class FileId(ctypes.Structure):
        _fields_ = [("volume", ctypes.c_ulonglong), ("identity", ctypes.c_ubyte * 16)]

    try:
        filesystem = ctypes.create_unicode_buffer(32)
        volume = kernel.GetVolumeInformationByHandleW
        volume.argtypes = [
            w.HANDLE,
            w.LPWSTR,
            w.DWORD,
            ctypes.POINTER(w.DWORD),
            ctypes.POINTER(w.DWORD),
            ctypes.POINTER(w.DWORD),
            w.LPWSTR,
            w.DWORD,
        ]
        if (
            not volume(handle, None, 0, None, None, None, filesystem, 32)
            or filesystem.value != "NTFS"
        ):
            return None
        info = FileId()
        get = kernel.GetFileInformationByHandleEx
        get.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
        if not get(handle, 18, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        birth = w.FILETIME()
        get_time = kernel.GetFileTime
        get_time.argtypes = [
            w.HANDLE,
            ctypes.POINTER(w.FILETIME),
            ctypes.POINTER(w.FILETIME),
            ctypes.POINTER(w.FILETIME),
        ]
        if not get_time(handle, ctypes.byref(birth), None, None):
            return None
        return json.dumps(
            {
                "v": 1,
                "machine": machine,
                "volume": info.volume,
                "id": bytes(info.identity).hex(),
                "birth": (birth.dwHighDateTime << 32) | birth.dwLowDateTime,
            },
            sort_keys=True,
        )
    finally:
        close(handle)


def _validate(path: Path, roots: tuple[Path, ...]) -> Path:
    if not path.is_absolute():
        raise ValueError("절대 경로의 외부 폴더를 선택하세요.")
    actual = path.resolve(strict=True)
    if any(actual.is_relative_to(root.resolve()) for root in roots):
        raise ValueError("OfficeFlow 관리 폴더가 아닌 외부 업무 폴더를 선택하세요.")
    if not actual.is_dir():
        raise ValueError("사용할 수 있는 폴더가 아닙니다.")
    return actual


def inspect(request: dict[str, Any]) -> dict[str, Any]:
    path = Path(request["path"])
    roots = tuple(Path(root) for root in request["roots"])
    expected = request.get("expected")
    try:
        actual = _validate(path, roots)
    except FileNotFoundError:
        if not expected or not request.get("search"):
            raise ValueError(
                "폴더를 찾지 못했습니다. 폴더 다시 지정을 사용하세요. 연결은 유지했습니다."
            ) from None
        saved = json.loads(expected)
        if saved.get("v") != 1 or saved.get("machine") != machine_key():
            raise ValueError(
                "이 PC에서 폴더를 확인할 수 없습니다. 폴더 다시 지정을 사용하세요. 연결은 유지했습니다."
            ) from None
        parent = _validate(path.parent, roots)
        parent_id = directory_identity(parent)
        if not parent_id or json.loads(parent_id)["volume"] != saved.get("volume"):
            raise ValueError("원래 드라이브를 확인할 수 없습니다. 연결은 유지했습니다.") from None
        started = time.monotonic()
        found: list[Path] = []
        with os.scandir(parent) as entries:
            for count, entry in enumerate(entries):
                if count >= SCAN_LIMIT or time.monotonic() - started > SCAN_SECONDS:
                    raise ValueError(
                        "폴더 확인 범위를 초과했습니다. 직접 다시 지정하세요. 연결은 유지했습니다."
                    ) from None
                candidate = Path(entry.path)
                if (
                    entry.is_symlink()
                    or candidate.is_junction()
                    or not entry.is_dir(follow_symlinks=False)
                ):
                    continue
                try:
                    candidate = _validate(candidate, roots)
                    if directory_identity(candidate) == expected:
                        found.append(candidate)
                except OSError:
                    continue
                except ValueError:
                    continue
        if len(found) != 1:
            raise ValueError(
                "원래 폴더를 확인하지 못했습니다. 폴더 다시 지정을 사용하세요. 연결은 유지했습니다."
            ) from None
        return {"path": str(found[0]), "identity": expected, "candidate": True}
    token = directory_identity(actual) if expected or request.get("capture") else None
    if expected and token != expected:
        raise ValueError(
            "연결한 폴더와 식별 정보가 다릅니다. 폴더 다시 지정을 사용하세요. 연결은 유지했습니다."
        )
    return {"path": str(actual), "identity": token, "candidate": False}


def probe(
    path: Path,
    roots: tuple[Path, ...],
    *,
    expected: str | None = None,
    capture: bool = False,
    search: bool = False,
    canceled: Callable[[], bool] = lambda: False,
    timeout: float = 5,
) -> dict[str, Any]:
    if canceled():
        raise ValueError("폴더 확인을 취소했습니다.")
    request = json.dumps(
        {
            "path": str(path),
            "roots": [str(p) for p in roots],
            "expected": expected,
            "capture": capture,
            "search": search,
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "officeflow.infrastructure.folder_identity"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    started = time.monotonic()
    try:
        first = True
        while True:
            if canceled() or time.monotonic() - started > timeout:
                raise ValueError(
                    "폴더 확인이 취소되었거나 시간이 초과되었습니다. 연결은 유지했습니다."
                )
            try:
                output, _ = process.communicate(request if first else None, timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                first = False
        if process.returncode:
            raise ValueError("폴더 확인을 마치지 못했습니다. 연결은 유지했습니다.")
        result: dict[str, Any] = json.loads(output)
        if "error" in result:
            raise ValueError(result["error"])
        return result
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


if __name__ == "__main__":
    try:
        response = inspect(json.load(sys.stdin))
    except Exception as error:
        response = {"error": str(error)}
    print(json.dumps(response, ensure_ascii=True))
