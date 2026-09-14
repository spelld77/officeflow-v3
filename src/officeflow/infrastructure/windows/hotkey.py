from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray, QCoreApplication, QTimer

logger = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000


@dataclass(frozen=True, slots=True)
class ParsedHotkey:
    modifiers: int
    virtual_key: int
    display: str


def parse_windows_hotkey(shortcut: str) -> ParsedHotkey:
    tokens = [token.strip() for token in shortcut.split("+") if token.strip()]
    if len(tokens) < 2:
        raise ValueError("전역 단축키는 보조 키와 일반 키를 함께 사용해야 합니다.")
    modifier_map = {
        "ctrl": (MOD_CONTROL, "Ctrl"),
        "control": (MOD_CONTROL, "Ctrl"),
        "alt": (MOD_ALT, "Alt"),
        "shift": (MOD_SHIFT, "Shift"),
        "win": (MOD_WIN, "Win"),
        "meta": (MOD_WIN, "Win"),
    }
    modifiers = MOD_NOREPEAT
    display_modifiers: list[str] = []
    key_token: str | None = None
    for token in tokens:
        normalized = token.casefold()
        if normalized in modifier_map:
            flag, display = modifier_map[normalized]
            if flag & modifiers:
                raise ValueError("전역 단축키에 같은 보조 키가 중복됐습니다.")
            modifiers |= flag
            display_modifiers.append(display)
        elif key_token is None:
            key_token = token.upper()
        else:
            raise ValueError("전역 단축키에는 일반 키를 하나만 지정할 수 있습니다.")
    if key_token is None or modifiers == MOD_NOREPEAT:
        raise ValueError("전역 단축키는 보조 키와 일반 키를 함께 사용해야 합니다.")
    if len(key_token) == 1 and key_token.isascii() and key_token.isalnum():
        virtual_key = ord(key_token)
    elif key_token.startswith("F") and key_token[1:].isdigit():
        function_number = int(key_token[1:])
        if not 1 <= function_number <= 24:
            raise ValueError("기능 키는 F1~F24만 사용할 수 있습니다.")
        virtual_key = 0x70 + function_number - 1
    else:
        raise ValueError("전역 단축키의 일반 키는 영문, 숫자 또는 F1~F24여야 합니다.")
    return ParsedHotkey(
        modifiers=modifiers,
        virtual_key=virtual_key,
        display="+".join([*display_modifiers, key_token]),
    )


class WindowsGlobalHotkey(QAbstractNativeEventFilter):
    def __init__(
        self,
        application: QCoreApplication,
        shortcut: str,
        callback: Callable[[], None],
        *,
        hotkey_id: int = 0x4F46,
    ) -> None:
        super().__init__()
        self._application = application
        self._parsed = parse_windows_hotkey(shortcut)
        self._callback = callback
        self._hotkey_id = hotkey_id
        self._registered = False
        self._user32: Any = None

    @property
    def shortcut(self) -> str:
        return self._parsed.display

    @property
    def registered(self) -> bool:
        return self._registered

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        if self._registered:
            return True
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.RegisterHotKey.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._user32.RegisterHotKey.restype = wintypes.BOOL
        registered = bool(
            self._user32.RegisterHotKey(
                None,
                self._hotkey_id,
                self._parsed.modifiers,
                self._parsed.virtual_key,
            )
        )
        if not registered:
            logger.warning("전역 단축키 %s 등록에 실패했습니다.", self.shortcut)
            return False
        self._application.installNativeEventFilter(self)
        self._registered = True
        return True

    def stop(self) -> None:
        if not self._registered:
            return
        self._application.removeNativeEventFilter(self)
        if self._user32 is not None:
            self._user32.UnregisterHotKey(None, self._hotkey_id)
        self._registered = False

    def nativeEventFilter(
        self,
        event_type: QByteArray | bytes | bytearray | memoryview[int],
        message: int,
    ) -> tuple[bool, int]:
        del event_type
        if sys.platform == "win32":
            native_message = wintypes.MSG.from_address(int(message))
            if native_message.message == WM_HOTKEY and native_message.wParam == self._hotkey_id:
                QTimer.singleShot(0, self._callback)
        return False, 0
