from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from officeflow.bootstrap.single_instance import SingleInstanceCoordinator
from officeflow.infrastructure.windows.hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    parse_windows_hotkey,
)
from officeflow.infrastructure.windows.startup import startup_command


def test_hotkey_parser_normalizes_supported_shortcut() -> None:
    parsed = parse_windows_hotkey("control + alt + o")

    assert parsed.display == "Ctrl+Alt+O"
    assert parsed.modifiers == MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
    assert parsed.virtual_key == ord("O")


@pytest.mark.parametrize("shortcut", ["O", "Ctrl+Alt", "Ctrl+Ctrl+O", "Ctrl+한"])
def test_hotkey_parser_rejects_unsafe_or_incomplete_shortcut(shortcut: str) -> None:
    with pytest.raises(ValueError):
        parse_windows_hotkey(shortcut)


def test_startup_command_launches_in_background() -> None:
    command = startup_command()

    assert "--background" in command
    assert "officeflow.main" in command or command.endswith('--background')


def test_single_instance_lock_rejects_secondary(tmp_path: Path) -> None:
    name = f"officeflow-test-{uuid4().hex}"
    lock_file = tmp_path / "officeflow.lock"
    primary = SingleInstanceCoordinator(name, lock_file=lock_file)
    secondary = SingleInstanceCoordinator(name, lock_file=lock_file)
    try:
        assert primary.acquire() is True
        assert secondary.acquire() is False
    finally:
        secondary.close()
        primary.close()
