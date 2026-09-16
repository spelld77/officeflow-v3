from __future__ import annotations

import tomllib
from pathlib import Path

from officeflow import __version__
from officeflow.presentation.help import HELP_FILENAME, user_help_path

ROOT = Path(__file__).resolve().parents[2]


def test_release_version_is_consistent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version_info = (ROOT / "packaging/windows/version_info.txt").read_text(encoding="utf-8")
    installer = (ROOT / "packaging/windows/OfficeFlow.iss").read_text(encoding="utf-8")

    assert __version__ == "3.0.0"
    assert project["project"]["version"] == __version__
    assert "FileVersion', '3.0.0'" in version_info
    assert '#define AppVersion "3.0.0"' in installer


def test_installer_is_per_user_upgrade_safe_and_preserves_data() -> None:
    installer = (ROOT / "packaging/windows/OfficeFlow.iss").read_text(encoding="utf-8")

    assert "AppId={{824ED7F8-1C70-4B62-A862-130A68B37C35}" in installer
    assert "PrivilegesRequired=lowest" in installer
    assert "DefaultDirName={localappdata}\\Programs\\OfficeFlow" in installer
    assert 'Name: "startup"' in installer
    assert "--remove-startup" in installer
    assert "[UninstallDelete]" not in installer


def test_release_build_verifies_package_and_installer_lifecycle() -> None:
    build_script = (ROOT / "scripts/build-release.ps1").read_text(encoding="utf-8")
    verify_script = (ROOT / "scripts/verify-installer.ps1").read_text(encoding="utf-8")

    assert "--smoke-test" in build_script
    assert "verify-installer.ps1" in build_script
    assert "SHA256SUMS.txt" in build_script
    assert "database\\migrations" in build_script
    assert 'Filter "icu*.dll"' in build_script
    assert verify_script.count("--smoke-test") == 2
    assert "preserve-through-upgrade.txt" in verify_script
    assert 'PATH = "$env:SystemRoot\\System32;$env:SystemRoot"' in verify_script


def test_user_help_is_available_in_development_tree() -> None:
    help_path = user_help_path()

    assert help_path.name == HELP_FILENAME
    assert help_path.is_file()
    content = help_path.read_text(encoding="utf-8")
    assert "OfficeFlow 2.6 데이터 가져오기" in content
    assert "%LOCALAPPDATA%\\OfficeFlow" in content
